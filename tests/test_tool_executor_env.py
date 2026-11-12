"""Tests for the environment of run_test / run_code child processes.

The child must NOT inherit the platform's own environment. An earlier
contract propagated SAGAAI_DATA_DIR so that child code could reach the same
database and history as the platform; that made every execution a potential
exfiltration path for provider keys and conversations. The contract is now
the opposite: the child gets an allowlist, its own HOME and only the project
on PYTHONPATH.
"""
import os
import subprocess
from unittest import mock

from dev_agent.sandbox_env import build_child_env
from dev_agent.tool_executor import ToolExecutor


class _FakePopen:
    """Minimal stand-in for subprocess.Popen used by sandbox_proc."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.returncode = 0
        self.pid = 424242
        self.stdout = _EmptyStream()
        self.stderr = _EmptyStream()
        _CAPTURED.append(dict(kwargs))

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        self.returncode = -9


class _EmptyStream:
    """A readable stream that immediately reports EOF."""

    def read(self, size=-1):
        return b""


_CAPTURED = []


class _FakeSubprocess:
    """The subset of the subprocess module used by the exec tools."""

    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL
    CREATE_NEW_PROCESS_GROUP = 0
    Popen = _FakePopen

    class TimeoutExpired(Exception):
        pass


def _run_with_capture(tool: str, **kwargs):
    """Call one exec tool with the child process faked; return the env used."""
    _CAPTURED.clear()
    te = ToolExecutor()
    with mock.patch("dev_agent.tool_executor.subprocess", _FakeSubprocess):
        result = getattr(te, tool)(**kwargs)
    env = _CAPTURED[0].get("env") if _CAPTURED else None
    return result, env or {}


def test_run_code_child_does_not_receive_platform_secrets(monkeypatch):
    """A provider key in the parent environment never reaches the child."""
    monkeypatch.setenv("SAGAAI_DEEPSEEK_KEY", "sk-should-not-leak")
    monkeypatch.setenv("SAGAAT_TOKEN", "also-not")
    result, env = _run_with_capture("run_code", code="print('hi')")
    assert result["ok"] is True
    assert "SAGAAI_DEEPSEEK_KEY" not in env
    assert "SAGAAT_TOKEN" not in env
    assert all("should-not-leak" not in str(value) for value in env.values())


def test_run_code_child_does_not_receive_data_dir(monkeypatch):
    """SAGAAI_DATA_DIR is not propagated: the child cannot open the database."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", "/tmp/secret-data-dir")
    result, env = _run_with_capture("run_code", code="print('hi')")
    assert result["ok"] is True
    assert "SAGAAI_DATA_DIR" not in env


def test_run_test_child_does_not_receive_data_dir(monkeypatch):
    """run_test uses the same allowlist as run_code."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", "/tmp/secret-data-dir")
    result, env = _run_with_capture("run_test", code="print('hi')")
    assert result["ok"] is True
    assert "SAGAAI_DATA_DIR" not in env


def test_child_home_is_not_the_user_home(monkeypatch):
    """The child gets its own HOME and TMPDIR inside the runtime area."""
    monkeypatch.setenv("HOME", "/home/real-user")
    result, env = _run_with_capture("run_code", code="print('hi')")
    assert result["ok"] is True
    assert env["HOME"] != "/home/real-user"
    assert env["TMPDIR"].startswith(env["HOME"])
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_pythonpath_points_at_the_project_only(tmp_path):
    """PYTHONPATH carries the project itself, never its parent directory."""
    env = build_child_env(project_root=tmp_path)
    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["PYTHONPATH"] != str(tmp_path.parent)


def test_build_child_env_blocks_extra_secrets():
    """Even an explicit extra mapping cannot reintroduce a platform secret."""
    env = build_child_env(project_root=None, extra={
        "SAGAAI_OPENAI_KEY": "sk-leak",
        "PATH": "/usr/bin",
    })
    assert "SAGAAI_OPENAI_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_the_tool_passes_the_isolated_default(monkeypatch):
    """An executor that was never stamped runs the filtered child."""
    monkeypatch.setenv("SAGAAI_YANDEXAI_KEY", "secret-value")
    result, env = _run_with_capture("run_code", code="print('hi')")
    assert result["ok"] is True
    assert "SAGAAI_YANDEXAI_KEY" not in env


def test_the_tool_honours_the_grant(monkeypatch):
    """A stamped full-access grant reaches the child environment."""
    monkeypatch.setenv("SAGAAI_YANDEXAI_KEY", "secret-value")
    _CAPTURED.clear()
    te = ToolExecutor()
    te._env_isolation = False
    with mock.patch("dev_agent.tool_executor.subprocess", _FakeSubprocess):
        result = te.run_code(code="print('hi')")
    assert result["ok"] is True
    env = _CAPTURED[0].get("env") or {}
    assert env["SAGAAI_YANDEXAI_KEY"] == "secret-value"


def test_the_tool_reads_the_grant_at_call_time(monkeypatch):
    """Flipping the grant between two calls changes the second child only."""
    monkeypatch.setenv("SAGAAI_YANDEXAI_KEY", "secret-value")
    _CAPTURED.clear()
    te = ToolExecutor()
    with mock.patch("dev_agent.tool_executor.subprocess", _FakeSubprocess):
        te.run_code(code="print('hi')")
        first = dict(_CAPTURED[0].get("env") or {})
        te._env_isolation = False
        te.run_code(code="print('hi')")
        second = dict(_CAPTURED[1].get("env") or {})
    assert "SAGAAI_YANDEXAI_KEY" not in first
    assert second["SAGAAI_YANDEXAI_KEY"] == "secret-value"

