# -*- coding: utf-8 -*-
"""tests/test_delegation_injection_inheritance.py - child safety-mode inheritance.

A delegated child runs with the prompt-injection protection mode of the run
that started it. Three facts make that work and are pinned here, because a
refactor can silently break each of them:

1. the mode is resolved ONCE at spawn time and frozen on the run record - a
   background child starts long after the parent step and must not read the
   live setting of whichever dialog happens to run then;
2. a resume continues the same unit of work, so it inherits the mode frozen
   in the source run instead of falling back to the default;
3. the model cannot set the mode: the name is absent from the tool-argument
   catalog, so the inheritance cannot be turned off from a tool call.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    SpawnRequest,
    STATUS_DONE,
    STOP_COMPLETED,
)


def _request(**kwargs):
    """Build a spawn request with only the fields a test cares about."""
    data = dict(agent_slug="dev_agent", request=AgentBrief(goal="audit"))
    data.update(kwargs)
    return SpawnRequest(**data)


@pytest.fixture
def svc(tmp_path):
    """A private service with its own store root and a recording runner."""
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))
    seen = []

    def _runner(**kwargs):
        seen.append(kwargs)
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED, summary="ok")

    service.set_child_runner(_runner)
    service.seen = seen
    return service


# ─── the contract default ─────────────────────────────────────────────────


def test_the_request_default_is_none_meaning_the_service_decides():
    """``None`` is not ``False``: an absent argument must not disable the gate."""
    assert _request().enable_injection_protection is None


def test_the_run_record_defaults_to_protection_on():
    """A record built without an argument is protected, never silently open."""
    assert S.RunRecord(run_id="r").enable_injection_protection is True


# ─── freezing at spawn time ───────────────────────────────────────────────


def test_the_parent_mode_is_frozen_on_the_run_record(svc):
    """The child runs with the mode it was started with, not with a default."""
    svc.ensure_run("root", thread_id="thread-1")

    out = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                           enable_injection_protection=False),
                  parent_config={"max_depth": 1, "max_active_subagents": 3})

    assert out["ok"] is True
    record = svc.get_record(out["result"]["run_id"])
    assert record.enable_injection_protection is False
    assert svc.seen[-1]["enable_injection_protection"] is False


def test_the_frozen_mode_reaches_the_durable_record(svc):
    """A resume after a restart reads the mode from the store, not memory."""
    svc.ensure_run("root", thread_id="thread-1")
    out = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                           enable_injection_protection=False),
                  parent_config={"max_depth": 1, "max_active_subagents": 3})

    stored = svc.record_from_store(out["result"]["run_id"])

    assert stored is not None
    assert stored["enable_injection_protection"] is False


def test_a_spawn_without_the_argument_keeps_protection_on(svc):
    """The safe default survives a caller that says nothing."""
    svc.ensure_run("root", thread_id="thread-1")

    out = svc.run(_request(parent_run_id="root", thread_id="thread-1"),
                  parent_config={"max_depth": 1, "max_active_subagents": 3})

    assert svc.get_record(out["result"]["run_id"]).enable_injection_protection is True
    assert svc.seen[-1]["enable_injection_protection"] is True


# ─── resume inheritance ───────────────────────────────────────────────────


def test_a_resume_inherits_the_mode_of_the_source_run(svc, tmp_path):
    """A resume continues the same unit of work, so it keeps its mode."""
    svc.ensure_run("root", thread_id="thread-1")
    first = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                             enable_injection_protection=False),
                    parent_config={"max_depth": 1, "max_active_subagents": 3})
    source_id = first["result"]["run_id"]

    resumed = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                               resume_run_id=source_id),
                      parent_config={"max_depth": 1, "max_active_subagents": 3})

    assert resumed["ok"] is True
    assert svc.get_record(resumed["result"]["run_id"]).enable_injection_protection is False


def test_an_explicit_argument_wins_over_the_inherited_mode(svc):
    """A caller that states the mode is not overruled by the source run."""
    svc.ensure_run("root", thread_id="thread-1")
    first = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                             enable_injection_protection=False),
                    parent_config={"max_depth": 1, "max_active_subagents": 3})
    source_id = first["result"]["run_id"]

    resumed = svc.run(_request(parent_run_id="root", thread_id="thread-1",
                               resume_run_id=source_id,
                               enable_injection_protection=True),
                      parent_config={"max_depth": 1, "max_active_subagents": 3})

    assert svc.get_record(resumed["result"]["run_id"]).enable_injection_protection is True


# ─── the runner seam ──────────────────────────────────────────────────────


def test_an_old_runner_without_the_parameter_still_works(tmp_path):
    """The runner is a test seam: a FIXED legacy signature must not break a spawn."""
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))

    def _old_runner(child_slug, brief, child_thread_id, run_id, parent_run_id,
                    depth, limits, deadline, base_dir, lang, service):
        # Deliberately fixed signature: neither parent_thread_id nor the mode.
        return AgentRunResult(run_id=run_id, status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED)

    service.set_child_runner(_old_runner)
    service.ensure_run("root", thread_id="thread-1")

    out = service.run(_request(parent_run_id="root", thread_id="thread-1"),
                      parent_config={"max_depth": 1, "max_active_subagents": 3})

    assert out["ok"] is True


# ─── the model cannot set the mode ────────────────────────────────────────


def test_the_mode_is_not_a_model_facing_argument():
    """A tool call must not be able to switch the child's gate off."""
    from dev_agent import universal_agent as UA

    for tool_name in ("spawn_agent", "resume_subagent"):
        spec = UA.WORKSPACE_TOOL_ARGS[tool_name]
        names = set(spec["required"]) | set(spec["optional"])
        assert "enable_injection_protection" not in names, tool_name


def test_the_spawn_signature_accepts_the_internal_argument():
    """The plumbing exists - it is simply not advertised to the model."""
    import inspect

    from core.orchestration import tools

    for fn in (tools.spawn_agent, tools.resume_subagent):
        assert "enable_injection_protection" in inspect.signature(fn).parameters


def test_spawn_agent_copies_the_value_into_the_request():
    """The tool layer passes the value through instead of swallowing it."""
    from core.orchestration import tools

    captured = {}

    class _Service:
        def run(self, req, **kwargs):
            captured["req"] = req
            return {"ok": True}

    import core.orchestrators as orch_mod

    original = orch_mod.get_orchestrator
    orch_mod.get_orchestrator = lambda slug: {"slug": slug, "config": {}}
    try:
        tools.spawn_agent(agent_slug="dev_agent", request=AgentBrief(goal="g"),
                          service=_Service(), enable_injection_protection=False)
    finally:
        orch_mod.get_orchestrator = original

    assert captured["req"].enable_injection_protection is False


# ─── the loop side: bypass is not a mode ──────────────────────────────────


class _Dispatcher:
    """Minimal dispatcher surface used by the delegation fan-out."""

    def __init__(self, slug="dev_agent"):
        self._slug = slug
        self.child_disabled_tools = set()

    def _current_orchestrator_slug(self):
        return self._slug


def _loop_state(**kwargs):
    from dev_agent import agent_loop as AL

    data = dict(task="test", thread_id="thread-1", run_id="run-1")
    data.update(kwargs)
    return AL.AgentLoopState(**data)


def _spawn_call():
    return {"tool": "spawn_agent",
            "args": {"agent_slug": "dev_agent", "request": {"goal": "audit"}}}


@pytest.fixture
def loop_env(monkeypatch):
    """Record what the fan-out hands to the tool layer."""
    import core.orchestration.tools as tools
    import core.orchestrators as orch_mod

    captured = []
    monkeypatch.setattr(orch_mod, "get_allowed_agents",
                        lambda slug="dev_agent": ["dev_agent"])
    monkeypatch.setattr(orch_mod, "get_allowed_assistants",
                        lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": {}})
    monkeypatch.setattr(orch_mod, "get_disabled_tools", lambda slug="dev_agent": [])

    def _spawn(agent_slug=None, request=None, inline_orchestrator=None, **kwargs):
        captured.append(kwargs.get("enable_injection_protection"))
        return {"ok": True}

    monkeypatch.setattr(tools, "spawn_agent", _spawn)
    return captured


def test_a_dialog_with_the_gate_off_spawns_an_unprotected_child(loop_env):
    """The whole point: the mode follows the parent, it is not a default."""
    from dev_agent import agent_loop as AL

    state = _loop_state(enable_injection_protection=False)
    AL.execute_delegation_calls([_spawn_call()], state=state,
                                dispatcher=_Dispatcher(), service=object())

    assert loop_env == [False]


def test_a_dialog_with_the_gate_on_spawns_a_protected_child(loop_env):
    """The default dialog keeps its protection - and passes it on."""
    from dev_agent import agent_loop as AL

    state = _loop_state()
    AL.execute_delegation_calls([_spawn_call()], state=state,
                                dispatcher=_Dispatcher(), service=object())

    assert loop_env == [True]


def test_the_one_step_bypass_is_not_inherited(loop_env):
    """Approving ONE payload is not a mode: the child stays protected.

    While the bypass is active the state flag reads False, so the bypass flag
    itself must decide - otherwise a child would be launched unprotected just
    because the user allowed viewing one withheld fragment.
    """
    from dev_agent import agent_loop as AL

    state = _loop_state(enable_injection_protection=False,
                        injection_protection_bypassed=True)
    AL.execute_delegation_calls([_spawn_call()], state=state,
                                dispatcher=_Dispatcher(), service=object())

    assert loop_env == [True]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
