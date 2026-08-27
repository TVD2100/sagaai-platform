# -*- coding: utf-8 -*-
"""
Regression tests for the orchestrator "Safe mode" toggle.

When the "Safe mode" checkbox is OFF, the agent must NOT stop for dangerous
operation confirmations. These tests pin the ToolExecutor behavior that backs
that contract.
"""
from __future__ import annotations

from dev_agent.tool_executor import ToolExecutor


# A payload that the danger classifier flags as dangerous but that is
# actually harmless when executed. subprocess.run with shell=True (a string
# command) is deliberately flagged by core.dangerous while "echo" is safe to
# run, so it lets us verify execution without side effects.
_DANGEROUS_BUT_HARMLESS = (
    'import subprocess\n'
    'print(subprocess.run("echo safe-mode-ok", shell=True, '
    'capture_output=True, text=True).stdout.strip())\n'
)


def test_safety_enabled_is_true_by_default():
    exec = ToolExecutor()
    assert exec._safety_enabled is True


def test_run_code_skips_confirmation_when_safety_disabled():
    exec = ToolExecutor()
    exec._safety_enabled = False

    result = exec.run_code(code=_DANGEROUS_BUT_HARMLESS)

    assert result.get("confirmation_required") is not True
    assert result.get("returncode") == 0
    assert "safe-mode-ok" in result.get("stdout", "")


def test_run_code_requires_confirmation_when_safety_enabled():
    exec = ToolExecutor()
    exec._safety_enabled = True

    result = exec.run_code(code=_DANGEROUS_BUT_HARMLESS)

    assert result.get("confirmation_required") is True
    assert result.get("ok") is False
    assert "returncode" not in result


def test_run_test_skips_confirmation_when_safety_disabled():
    exec = ToolExecutor()
    exec._safety_enabled = False

    result = exec.run_test(code='print(2 + 2)')

    assert result.get("confirmation_required") is not True
    assert result.get("returncode") == 0
    assert "4" in result.get("stdout", "")


def test_run_test_requires_confirmation_when_safety_enabled():
    exec = ToolExecutor()
    exec._safety_enabled = True

    result = exec.run_test(code=_DANGEROUS_BUT_HARMLESS)

    assert result.get("confirmation_required") is True
    assert result.get("ok") is False
    assert "returncode" not in result
