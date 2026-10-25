# -*- coding: utf-8 -*-
"""Scenario tests: a delegated child runs with the parent's safety mode.

The unit tests pin the plumbing. This file walks the four situations a user
actually creates and asserts what the running system does in each of them:

1. the user turned the Safety checkbox OFF, then asked for a subagent - the
   child must read files without the gate stopping it;
2. the user kept Safety ON (the default) - the child must stay protected;
3. a resumable child is continued later - the continuation keeps the mode of
   the run it continues, even when the caller says nothing;
4. an explicit caller argument overrides the inherited mode.

Each scenario goes through the public entry points (the service, the tool
layer, the loop fan-out), never through an internal helper.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    SpawnRequest,
    STATUS_DONE,
    STOP_COMPLETED,
)

CONFIG = {"max_depth": 1, "max_active_subagents": 3,
          "subagent_max_steps": 20, "subagent_timeout_seconds": 30}


@pytest.fixture
def service(tmp_path):
    """A real service with an isolated store and a child that records its mode."""
    svc = S.OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    modes = []

    def _runner(**kwargs):
        modes.append(kwargs.get("enable_injection_protection"))
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED, summary="ok")

    svc.set_child_runner(_runner)
    svc.modes = modes
    return svc


def _spawn(service, *, parent_run_id="dialog-1", **kwargs):
    """Ask for a child the way the dialog would."""
    req = SpawnRequest(agent_slug="dev_agent", request=AgentBrief(goal="audit"),
                       parent_run_id=parent_run_id, thread_id="dialog-1",
                       **kwargs)
    return service.run(req, parent_config=CONFIG)


# ─── 1. the user turned the gate off ──────────────────────────────────────


def test_scenario_safety_off_child_reads_without_the_gate(service):
    """Given Safety is OFF and the user asks for a subagent,
    when  the dialog spawns it,
    then  the child runs without prompt-injection protection.
    """
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    spawned = _spawn(service, enable_injection_protection=False)

    assert spawned["ok"] is True
    run_id = spawned["result"]["run_id"]
    assert service.modes == [False]
    assert service.get_record(run_id).enable_injection_protection is False


# ─── 2. the default dialog keeps the gate ─────────────────────────────────


def test_scenario_safety_on_child_stays_protected(service):
    """Given a dialog with Safety ON (the default),
    when  it spawns a subagent without stating a mode,
    then  the child is protected - the gate never disappears by accident.
    """
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    spawned = _spawn(service)

    run_id = spawned["result"]["run_id"]
    assert service.modes == [True]
    assert service.get_record(run_id).enable_injection_protection is True


# ─── 3. a continuation keeps the mode of the run ──────────────────────────


def test_scenario_resumed_child_keeps_the_original_mode(service):
    """Given an unprotected child that stopped half-way,
    when  the dialog continues it later without stating a mode,
    then  the continuation is unprotected too - it is the same unit of work.
    """
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    first = _spawn(service, enable_injection_protection=False)
    source_id = first["result"]["run_id"]

    resumed = _spawn(service, resume_run_id=source_id)

    assert resumed["ok"] is True
    assert service.get_record(resumed["result"]["run_id"]).enable_injection_protection is False
    assert service.modes == [False, False]


# ─── 4. an explicit mode wins ─────────────────────────────────────────────


def test_scenario_explicit_mode_overrides_the_source_run(service):
    """Given an unprotected child,
    when  the caller continues it while explicitly asking for protection,
    then  the continuation is protected - an explicit decision is not overruled.
    """
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    first = _spawn(service, enable_injection_protection=False)
    source_id = first["result"]["run_id"]

    resumed = _spawn(service, resume_run_id=source_id,
                     enable_injection_protection=True)

    assert service.get_record(resumed["result"]["run_id"]).enable_injection_protection is True
    assert service.modes == [False, True]


# ─── end-to-end through the loop fan-out ──────────────────────────────────


def test_scenario_the_loop_hands_the_dialog_mode_to_the_tool(monkeypatch):
    """Given a live dialog state with Safety OFF,
    when  the loop executes a spawn_agent call,
    then  the tool receives exactly that mode - the chain has no gap.
    """
    import core.orchestration.tools as tools_mod
    import core.orchestrators as orch_mod
    from dev_agent import agent_loop as AL

    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": ["dev_agent"])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": CONFIG})
    monkeypatch.setattr(orch_mod, "get_disabled_tools", lambda slug="dev_agent": [])

    seen = []

    def _fake_spawn(agent_slug=None, request=None, inline_orchestrator=None, **kwargs):
        seen.append(kwargs.get("enable_injection_protection"))
        return {"ok": True}

    monkeypatch.setattr(tools_mod, "spawn_agent", _fake_spawn)

    class _Dispatcher:
        child_disabled_tools = set()

        def _current_orchestrator_slug(self):
            return "dev_agent"

    state = AL.AgentLoopState(task="audit", thread_id="dialog-1",
                              run_id="dialog-1",
                              enable_injection_protection=False)
    calls = [{"tool": "spawn_agent",
              "args": {"agent_slug": "dev_agent", "request": {"goal": "audit"}}}]

    AL.execute_delegation_calls(calls, state=state, dispatcher=_Dispatcher(),
                                service=object())

    assert seen == [False]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
