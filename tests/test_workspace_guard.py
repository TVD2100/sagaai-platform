# -*- coding: utf-8 -*-
"""tests/test_workspace_guard.py - unit tests for the empty-workspace guard.

The guard (``dev_agent.tool_executor.workspace_guard_error``) protects the
"no folder selected" state: file tools refuse to run instead of touching an
arbitrary or neutral folder, while dialog-management tools stay callable.

Covers:
  * the pure guard helper: file tools blocked, code-mode run_test/run_code
    allowed, their path mode blocked, unrelated tools untouched;
  * dispatch through the core ToolExecutor: every guarded core file tool
    returns the structured ``workspace_not_selected`` error;
  * dispatch through UniversalDevAgent: core and workspace-layer file tools
    are blocked the same way;
  * run_code / run_test in code mode run their child process with the
    NEUTRAL root as cwd; path mode is blocked;
  * after set_workspace the same tools execute normally.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config  # noqa: E402
from dev_agent import workspace_binding as wb  # noqa: E402
from dev_agent.tool_executor import (  # noqa: E402
    ToolExecutor,
    workspace_guard_error,
)
from dev_agent.universal_agent import UniversalDevAgent  # noqa: E402


FILE_TOOLS = [
    "read_file", "list_files", "propose_file", "apply_patch", "verify_file",
    "create_backup", "restore_backup", "show_history", "search_in_files",
    "scan_folder", "assess_workspace", "build_project_map",
    "write_project_map", "write_doc", "read_doc", "snapshot_all",
    "restore_all", "list_snapshots",
]

CORE_CASES = {
    "read_file": {"path": "x.txt"},
    "list_files": {},
    "propose_file": {"path": "x.txt", "content": "y = 1"},
    "apply_patch": {"path": "x.txt", "edits": []},
    "verify_file": {"path": "x.txt"},
    "create_backup": {"path": "x.txt"},
    "restore_backup": {"path": "x.txt"},
    "show_history": {"path": "x.txt"},
}

WORKSPACE_LAYER_CASES = {
    "search_in_files": {"query": "x"},
    "scan_folder": {},
    "assess_workspace": {},
    "build_project_map": {},
    "write_project_map": {"responsibilities": {}},
    "write_doc": {"doc": "spec", "content": "x"},
    "read_doc": {"doc": "map"},
    "snapshot_all": {},
    "list_snapshots": {},
    "restore_all": {"snapshot_id": "missing"},
}

CODE_PRINT_CWD = """import os
print(os.getcwd())
"""


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Own temp data dir: workspace-history writes never hit the real DB."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    import storage.db as db_mod
    db_mod.reset_engine()
    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)
    yield
    db_mod.reset_engine()


@pytest.fixture(autouse=True)
def clean_config_state():
    """Save/restore the process-global config and the binding registry."""
    state = config.snapshot_state()
    with wb.sync_lock():
        old_registry = dict(wb._REGISTRY)
        wb._REGISTRY.clear()
    yield
    config.restore_state(state)
    with wb.sync_lock():
        wb._REGISTRY.clear()
        wb._REGISTRY.update(old_registry)


@pytest.fixture
def empty_state():
    """Switch the process into the empty (no folder selected) state."""
    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False, selected=False)
    assert config.WORKSPACE_SELECTED is False
    return config.NEUTRAL_ROOT


# -- pure guard helper ---------------------------------------------------------

def test_guard_blocks_every_file_tool(empty_state):
    for tool in FILE_TOOLS:
        err = workspace_guard_error(tool, {"path": "x.txt"})
        assert err is not None, tool
        assert err["ok"] is False
        assert err["workspace_not_selected"] is True
        assert tool in err["error"]
        assert "set_workspace" in err["suggestion"]


def test_guard_allows_non_file_tools(empty_state):
    for tool in ("current_workspace", "set_workspace", "set_target_file",
                 "list_recent_workspaces", "web_search"):
        assert workspace_guard_error(tool, {}) is None, tool


def test_guard_run_code_run_test_modes(empty_state):
    assert workspace_guard_error("run_code", {"code": "print(1)"}) is None
    assert workspace_guard_error("run_test", {"code": "print(1)"}) is None
    assert workspace_guard_error("run_code", {"path": "x.py"}) is not None
    assert workspace_guard_error("run_test", {"path": "tests/x.py"}) is not None
    # No args at all: the guard passes; the tool itself reports the missing mode.
    assert workspace_guard_error("run_code", {}) is None


def test_guard_inactive_when_workspace_selected(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    config.apply_paths(str(ws), create_dirs=False)
    assert config.WORKSPACE_SELECTED is True
    for tool in FILE_TOOLS:
        assert workspace_guard_error(tool, {"path": "x.txt"}) is None, tool
    assert workspace_guard_error("run_code", {"path": "x.py"}) is None


# -- dispatch: core ToolExecutor ----------------------------------------------

def test_core_dispatch_blocks_file_tools(empty_state):
    executor = ToolExecutor()
    for tool, args in CORE_CASES.items():
        res = executor.dispatch(tool, args)
        assert res.get("ok") is False, tool
        assert res.get("workspace_not_selected") is True, tool
        assert res.get("suggestion"), tool


# -- dispatch: UniversalDevAgent ----------------------------------------------

def test_universal_dispatch_blocks_core_file_tools(empty_state):
    agent = UniversalDevAgent()
    for tool, args in CORE_CASES.items():
        res = agent.dispatch(tool, args)
        assert res.get("workspace_not_selected") is True, tool


def test_universal_dispatch_blocks_workspace_file_tools(empty_state):
    agent = UniversalDevAgent()
    for tool, args in WORKSPACE_LAYER_CASES.items():
        res = agent.dispatch(tool, args)
        assert res.get("ok") is False, tool
        assert res.get("workspace_not_selected") is True, tool


def test_universal_dispatch_path_mode_blocked(empty_state):
    agent = UniversalDevAgent()
    for tool, args in (("run_code", {"path": "script.py"}),
                       ("run_test", {"path": "tests/test_x.py"})):
        res = agent.dispatch(tool, args)
        assert res.get("workspace_not_selected") is True, tool


# -- allowed child processes run with a neutral cwd ----------------------------

def test_run_code_code_mode_uses_neutral_cwd(empty_state):
    agent = UniversalDevAgent()
    agent.core._safety_enabled = False  # deterministic: skip the danger scan
    res = agent.dispatch("run_code", {"code": CODE_PRINT_CWD})
    assert res.get("ok") is True, res
    assert res["stdout"].strip() == str(config.NEUTRAL_ROOT)


def test_run_test_code_mode_uses_neutral_cwd(empty_state):
    agent = UniversalDevAgent()
    agent.core._safety_enabled = False
    res = agent.dispatch("run_test", {"code": CODE_PRINT_CWD})
    assert res.get("ok") is True, res
    assert res["stdout"].strip() == str(config.NEUTRAL_ROOT)


# -- recovery: after the user picks a folder ----------------------------------

def test_tools_work_after_set_workspace(empty_state, tmp_path):
    agent = UniversalDevAgent()
    blocked = agent.dispatch("read_file", {"path": "a.txt"})
    assert blocked.get("workspace_not_selected") is True

    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "a.txt").write_text("hello", encoding="utf-8")

    switch = agent.dispatch("set_workspace", {"path": str(folder)})
    assert switch.get("ok") is True, switch

    read = agent.dispatch("read_file", {"path": "a.txt"})
    assert read.get("ok") is True, read
    assert "hello" in read["content"]

    scan = agent.dispatch("scan_folder", {})
    assert scan.get("ok") is True

    search = agent.dispatch("search_in_files", {"query": "hello"})
    assert search.get("ok") is True
    assert search.get("match_count", 0) >= 1
