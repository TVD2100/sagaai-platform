# -*- coding: utf-8 -*-
"""tests/scenarios/test_execution_preferences_scenario.py - the switches, felt.

Each scenario starts with what the user chose in the toolbar and ends with
what the execution tool actually did. The runner is stubbed so the scenario
measures the decision, not a machine's isolation providers:

  Scenario 1 - both switches off: a snippet that used to park now runs, and
               the run reaches the runner under the off profile.
  Scenario 2 - sandbox off but confirmations on: the same snippet still
               parks. The two switches are independent.
  Scenario 3 - confirmations off but sandbox on: the snippet runs and the
               run keeps the isolated profile.
  Scenario 4 - nothing configured and an ordinary build command: it runs.
               This is the case that made an agent create wrapper scripts.
  Scenario 5 - a delegated child carries both choices of its employee.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.sandbox import resolve
from core.sandbox.contracts import SandboxResult
from core.sandbox.profiles import DEFAULT_PROFILE, OFF

#: A snippet the scanner refuses: it reaches outside the project and deletes.
#: It never executes in these scenarios - the runner is stubbed - but it is
#: the exact text the gate has to judge.
DANGEROUS_CODE = "import subprocess\nsubprocess.run(['rm', '-rf', '/tmp/x'])\n"

#: An ordinary command of a project cycle.
BUILD_CODE = "import subprocess\nsubprocess.run(['pytest', '--version'])\n"


@pytest.fixture()
def employee(monkeypatch):
    """Install an employee record and return a setter for its config."""
    state = {"config": {}}

    def fake_get_orchestrator(slug):
        return {"slug": slug, "config": dict(state["config"])}

    monkeypatch.setattr("core.orchestrators.get_orchestrator", fake_get_orchestrator)

    def set_config(**values):
        state["config"] = dict(values)

    return set_config


class StubRunner:
    """Records the request instead of spawning a child process."""

    def __init__(self):
        self.requests = []

    def execute(self, request, profile=None, timeout_s=None, subprocess_module=None):
        self.requests.append(request)
        return SandboxResult(ok=True, provider="stub", isolation="process",
                             returncode=0, stdout="scenario-ok", duration_ms=2)


@pytest.fixture()
def runner(monkeypatch):
    """Replace the sandbox runner and return the recording stub."""
    from core.sandbox import runner as runner_module

    stub = StubRunner()
    monkeypatch.setattr(runner_module, "execute", stub.execute)
    return stub


def _executor():
    """Return a real tool executor behind the dispatcher shape ``apply_to`` wants."""
    from dev_agent.tool_executor import ToolExecutor

    class _Dispatcher:
        def __init__(self):
            self.core = ToolExecutor()

    return _Dispatcher()


# ── scenarios ────────────────────────────────────────────────────────────────


def test_scenario_both_switches_off_let_the_snippet_run(employee, runner):
    """Scenario 1: the user turned isolation and confirmations off.

    Given an employee whose sandbox and confirmation switches are both off,
    when  the agent asks to run a snippet the scanner considers dangerous,
    then  no confirmation is demanded, the snippet reaches the runner, and
    the run carries the off profile.
    """
    employee(chat_sandbox=False, chat_safety_mode=False)
    dispatcher = _executor()
    resolve.apply_to(dispatcher, "dev_agent")

    result = dispatcher.core.run_code(code=DANGEROUS_CODE)

    assert "confirmation_required" not in result, result
    assert result["ok"] is True
    assert result["stdout"] == "scenario-ok"
    assert len(runner.requests) == 1
    assert runner.requests[0].profile == OFF


def test_scenario_a_cleared_sandbox_keeps_the_confirmation(employee, runner):
    """Scenario 2: only isolation was switched off.

    Given an employee with the sandbox off but confirmations still on,
    when  the agent asks to run a dangerous snippet,
    then  the run parks for confirmation and nothing reaches the runner.
    """
    employee(chat_sandbox=False, chat_safety_mode=True)
    dispatcher = _executor()
    resolve.apply_to(dispatcher, "dev_agent")

    result = dispatcher.core.run_code(code=DANGEROUS_CODE)

    assert result.get("confirmation_required") is True, result
    assert runner.requests == []


def test_scenario_a_cleared_confirmation_keeps_the_isolation(employee, runner):
    """Scenario 3: only confirmations were switched off.

    Given an employee with confirmations off and the sandbox on,
    when  the agent asks to run a dangerous snippet,
    then  it runs, and the run still carries the isolated default profile.
    """
    employee(chat_sandbox=True, chat_safety_mode=False)
    dispatcher = _executor()
    resolve.apply_to(dispatcher, "dev_agent")

    result = dispatcher.core.run_code(code=DANGEROUS_CODE)

    assert result["ok"] is True, result
    assert len(runner.requests) == 1
    assert runner.requests[0].profile == DEFAULT_PROFILE


def test_scenario_an_ordinary_build_command_runs_out_of_the_box(employee, runner):
    """Scenario 4: nothing is configured and the agent runs a normal command.

    Given an employee that never touched the toolbar switches,
    when  the agent runs pytest through subprocess,
    then  it runs without a confirmation - the ordinary cycle is not gated.
    """
    employee()
    dispatcher = _executor()
    resolve.apply_to(dispatcher, "dev_agent")

    result = dispatcher.core.run_code(code=BUILD_CODE)

    assert "confirmation_required" not in result, result
    assert result["ok"] is True


def test_scenario_a_delegated_child_inherits_both_choices(employee, monkeypatch):
    """Scenario 5: the work is delegated, the settings follow.

    Given an employee with both switches off,
    when  a child dispatcher is built for a delegated run,
    then  the child carries the off profile and the cleared gate, exactly as
    the dialog that launched it does.
    """
    import dev_agent.universal_agent as universal
    from core.orchestration import child_runner

    class FakeCore:
        def __init__(self):
            self._sandbox_profile = ""
            self._safety_enabled = True

        def set_send_request(self, fn):
            self.send_request = fn

    class FakeDispatcher:
        def __init__(self, *args, **kwargs):
            self.core = FakeCore()

        def attach_orchestrator(self, slug):
            self.slug = slug

    monkeypatch.setattr(universal, "UniversalDevAgent", FakeDispatcher)
    employee(chat_sandbox=False, chat_safety_mode=False)

    child = child_runner.build_child_dispatcher(
        slug="dev_agent", base_dir=None, child_thread_id="child-1", depth=1,
        limits={}, strong_assistant={}, lang="English", disabled_tools=set(),
    )

    assert child.core._sandbox_profile == OFF
    assert child.core._safety_enabled is False

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
