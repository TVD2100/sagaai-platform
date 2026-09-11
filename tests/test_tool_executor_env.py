"""Tests for SAGAAI_DATA_DIR propagation into run_test/run_code subprocesses.

Child processes spawned by run_test/run_code must resolve runtime data
(DB, history, connectors) to the SAME DATA_DIR as the platform process,
even when the variable is absent from os.environ at call time.
"""
import os
import subprocess
from unittest import mock

from core import paths as core_paths
from dev_agent.tool_executor import ToolExecutor


def _fake_run():
    captured = {}

    def run(*args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(list(args[0]), 0, "ok", "")

    return captured, run


def test_run_code_propagates_sagaai_data_dir():
    """run_code children receive SAGAAI_DATA_DIR even when it is absent from os.environ."""
    captured, runner = _fake_run()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SAGAAI_DATA_DIR", None)
        te = ToolExecutor()
        with mock.patch("dev_agent.tool_executor.subprocess.run", side_effect=runner):
            result = te.run_code(code="print('hi')")
        assert result["ok"] is True
    assert captured["env"].get("SAGAAI_DATA_DIR") == str(core_paths.DATA_DIR)


def test_run_test_propagates_sagaai_data_dir():
    """run_test children receive SAGAAI_DATA_DIR even when it is absent from os.environ."""
    captured, runner = _fake_run()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SAGAAI_DATA_DIR", None)
        te = ToolExecutor()
        with mock.patch("dev_agent.tool_executor.subprocess.run", side_effect=runner):
            result = te.run_test(code="print('hi')")
        assert result["ok"] is True
    assert captured["env"].get("SAGAAI_DATA_DIR") == str(core_paths.DATA_DIR)
