# -*- coding: utf-8 -*-
"""tests/scenarios/test_full_access_scenario.py - user-level access scenarios.

Scenarios walk the PUBLIC execution path ``ToolExecutor.run_code`` with the
real process provider, the way a user experiences the access setting:

  Scenario 1 - an isolated run does NOT see the platform environment, so a
               provider key stored there never reaches the child.
  Scenario 2 - a full-access run sees the same key, so a script that calls a
               provider API works without the key being pasted into it.
  Scenario 3 - the value the child prints is redacted from the tool result,
               so turning access on does not leak the key into the dialog.
  Scenario 4 - turning the setting back to isolated closes the environment
               again on the very next run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

_KEY_NAME = "SAGAAI_TEST_PROVIDER_KEY"
_KEY_VALUE = "sk-scenario-0123456789abcdef"


@pytest.fixture()
def platform_env(monkeypatch):
    """Put a provider-shaped key into the platform environment."""
    monkeypatch.setenv(_KEY_NAME, _KEY_VALUE)
    return _KEY_NAME


def _executor(monkeypatch, workspace: Path, full_access: bool):
    """Return a ToolExecutor that runs in *workspace* with the given access.

    The execution profile is fixed to ``off`` because that is what the
    resolver selects together with the grant: the scenario is about the
    environment, not about which isolation this machine can honour.
    """
    from core.sandbox.profiles import OFF
    from dev_agent import config
    from dev_agent.tool_executor import ToolExecutor

    monkeypatch.setattr(config, "PROJECT_ROOT", str(workspace), raising=False)
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True, raising=False)
    executor = ToolExecutor()
    executor._env_isolation = not full_access
    executor._sandbox_profile = OFF
    executor._safety_enabled = False
    return executor


def test_scenario_isolated_run_never_sees_the_platform_key(monkeypatch, tmp_path, platform_env):
    """Scenario 1: an isolated child cannot read the platform environment.

    Given a provider key in the platform environment,
    when  the user runs code that prints that variable,
    then  the child sees an empty value - the environment was built, not
    inherited.
    """
    executor = _executor(monkeypatch, tmp_path, full_access=False)

    result = executor.run_code("import os; print('seen=' + os.environ.get(%r, ''))" % platform_env)

    assert result.get("ok") is True, result
    assert result.get("stdout", "").strip() == "seen=", result
    assert _KEY_VALUE not in str(result)


def test_scenario_full_access_run_reaches_the_platform_key(monkeypatch, tmp_path, platform_env):
    """Scenario 2: the grant hands the platform environment to the child.

    Given the same key and the full-access setting,
    when  the user runs a script that needs it,
    then  the child reads the real value, so a provider call needs no
    pasted credentials.
    """
    executor = _executor(monkeypatch, tmp_path, full_access=True)

    result = executor.run_code("import os; print('len=' + str(len(os.environ.get(%r, ''))))" % platform_env)

    assert result.get("ok") is True, result
    assert ("len=" + str(len(_KEY_VALUE))) in result.get("stdout", ""), result


def test_scenario_the_printed_key_is_redacted_from_the_result(monkeypatch, tmp_path, platform_env):
    """Scenario 3: a printed key never reaches the dialog.

    Given a full-access run,
    when  the child prints the whole environment line,
    then  the tool result carries the variable NAME but not its value - the
    grant lets the code use the key, not the model read it.
    """
    executor = _executor(monkeypatch, tmp_path, full_access=True)

    result = executor.run_code("import os; print(%r + '=' + os.environ[%r])"
                               % (platform_env, platform_env))

    assert result.get("ok") is True, result
    assert platform_env in result.get("stdout", ""), result
    assert _KEY_VALUE not in str(result), result


def test_scenario_turning_the_grant_off_closes_the_environment_again(monkeypatch, tmp_path, platform_env):
    """Scenario 4: the setting is read per run, not cached.

    Given a workspace that just ran with full access,
    when  the user switches back to isolated,
    then  the next run cannot read the key any more.
    """
    executor = _executor(monkeypatch, tmp_path, full_access=True)
    first = executor.run_code("import os; print('len=' + str(len(os.environ.get(%r, ''))))" % platform_env)
    assert ("len=" + str(len(_KEY_VALUE))) in first.get("stdout", ""), first

    executor._env_isolation = True
    second = executor.run_code("import os; print('seen=' + os.environ.get(%r, ''))" % platform_env)

    assert second.get("ok") is True, second
    assert second.get("stdout", "").strip() == "seen=", second


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
