"""Tests for the private HOME of a child process.

A run needs its own HOME and TMPDIR, but nothing needs them afterwards: left
behind, they accumulate one cache tree per execution and quietly fill the
disk. ``discard_env_home`` removes only a directory this module created, so a
caller that supplied its own home keeps control over it.
"""
from __future__ import annotations

import os
from pathlib import Path

from dev_agent.sandbox_env import AUTO_HOME_PREFIX, build_child_env, discard_env_home
from dev_agent.tools.exec import ExecTools


class _Deps:
    """Minimal dependency bundle for an executor built without __init__."""

    class config:  # noqa: D106 - attribute holder
        DEFAULT_ENCODING = "utf-8"

    subprocess = None


def _bare_executor() -> ExecTools:
    """Return an executor whose dependency bundle needs no runtime setup."""
    tools = ExecTools.__new__(ExecTools)
    tools.deps = lambda: _Deps()
    return tools


def test_build_child_env_creates_a_prefixed_home():
    """A run gets a home whose name marks it as ours."""
    env = build_child_env(project_root=None)
    home = Path(env["HOME"])
    try:
        assert home.name.startswith(AUTO_HOME_PREFIX)
        assert home.is_dir()
    finally:
        discard_env_home(env)


def test_discard_env_home_removes_the_created_home():
    """The directory is gone after the run, and the call reports it."""
    env = build_child_env(project_root=None)
    home = Path(env["HOME"])
    assert discard_env_home(env) is True
    assert not home.exists()


def test_discard_env_home_keeps_a_caller_supplied_home(tmp_path):
    """A home the caller passed in belongs to the caller, not to the run."""
    custom = tmp_path / "my-home"
    custom.mkdir()
    env = build_child_env(project_root=None, sandbox_home=custom)
    assert discard_env_home(env) is False
    assert custom.is_dir()


def test_discard_env_home_is_safe_without_a_home():
    """An absent or empty environment is not an error."""
    assert discard_env_home(None) is False
    assert discard_env_home({}) is False


def test_run_command_removes_the_child_home(monkeypatch):
    """The execution path itself cleans up, not only the helper on its own."""
    seen = {}

    def fake_run_under_sandbox(self, tool, cmd, env, timeout_s, code_hash):
        seen["home"] = env["HOME"]
        seen["exists_during_run"] = os.path.isdir(env["HOME"])
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "",
                "duration_ms": 0, "log_path": ""}

    monkeypatch.setattr(ExecTools, "_run_under_sandbox", fake_run_under_sandbox)
    tools = _bare_executor()

    result = tools._run_command("run_code", code="print(1)", path=None,
                                script_path=None, timeout_s=5, code_hash="abc")

    assert result["ok"] is True
    assert seen["exists_during_run"] is True
    assert not os.path.exists(seen["home"])
