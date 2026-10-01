# -*- coding: utf-8 -*-
"""tests/scenarios/test_recent_workspace_scopes_scenario.py - user-level
scenarios for the per-orchestrator "recent folders" menu (isolation v2).

Scenarios (given -> when -> then), walking the entry points the platform
actually uses: the ``list_recent_workspaces`` tool on ToolExecutor (what the
agent calls at Stage 0 of a fresh dialog) and the workspace-switch history
write behind ``set_workspace``:

  Scenario 1 - a teacher_assistant dialog never sees folders that only
               DevAgent dialogs used, and vice versa.
  Scenario 2 - platform folders (the SagaAI install root) are hidden from
               a teacher_assistant menu but stay available to dev_agent.
  Scenario 3 - switching the workspace from a teacher dialog records the
               folder into the teacher shelf only (scoped history write).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Temporary DATA_DIR that isolates DB and history from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_recent_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()

    yield tmp

    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_config_state():
    """Save/restore the process-global config and the binding registry."""
    from dev_agent import config
    from dev_agent import workspace_binding as wb

    state = config.snapshot_state()
    with wb.sync_lock():
        old_registry = dict(wb._REGISTRY)
        wb._REGISTRY.clear()
    yield
    config.restore_state(state)
    with wb.sync_lock():
        wb._REGISTRY.clear()
        wb._REGISTRY.update(old_registry)


def _menu_paths(executor):
    """Return display paths of a list_recent_workspaces tool result."""
    res = executor.list_recent_workspaces()
    assert res.get("ok") is True
    return [p["path"] for p in res.get("projects", [])]


def test_scenario_teacher_menu_excludes_devagent_folders(tmp_path):
    """Scenario 1: menus never mix folders of different orchestrators."""
    from dev_agent.tool_executor import ToolExecutor
    from core.threads_devagent import create_devagent_thread

    dev_proj = tmp_path / "dev_only"
    dev_proj.mkdir()
    te_proj = tmp_path / "teacher_proj"
    te_proj.mkdir()

    create_devagent_thread(
        title="dev dialog", orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent", workspace=str(dev_proj),
    )
    create_devagent_thread(
        title="teacher dialog", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=str(te_proj),
    )

    # when: both dialogs ask for their recent folders at Stage 0
    teacher = ToolExecutor()
    teacher._orchestrator_slug = "teacher_assistant"
    teacher_paths = _menu_paths(teacher)

    dev = ToolExecutor()
    dev_paths = _menu_paths(dev)

    # then: each menu contains exactly its own dialog's folder
    assert teacher_paths == [str(te_proj.resolve())]
    assert str(dev_proj.resolve()) not in teacher_paths
    assert dev_paths == [str(dev_proj.resolve())]
    assert str(te_proj.resolve()) not in dev_paths


def test_scenario_platform_folder_hidden_from_teacher(tmp_path):
    """Scenario 2: the platform root is not suggested to other employees."""
    import dev_agent.config as dagent_config
    from dev_agent.tool_executor import ToolExecutor
    from core.threads_devagent import create_devagent_thread

    install = str(Path(dagent_config.INSTALL_ROOT).resolve())
    # given: a teacher dialog left over from older builds carries the
    # platform root in its thread meta (the historical leak)...
    create_devagent_thread(
        title="legacy dialog", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=install,
    )
    # ...while a DevAgent dialog legitimately works on the platform itself
    create_devagent_thread(
        title="platform dialog", orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent", workspace=install,
    )

    teacher = ToolExecutor()
    teacher._orchestrator_slug = "teacher_assistant"

    # when / then: the platform root never appears in the teacher menu
    assert _menu_paths(teacher) == []

    # while dev_agent still sees platform folders (it develops the platform)
    dev = ToolExecutor()
    assert install in _menu_paths(dev)


def test_scenario_switch_records_into_own_scope(tmp_path):
    """Scenario 3: a folder picked in a dialog lands in ITS shelf only."""
    from dev_agent.universal_agent import UniversalDevAgent
    from dev_agent.tool_executor import ToolExecutor

    proj = tmp_path / "picked_project"
    proj.mkdir()
    picked = str(proj.resolve())

    # given: a teacher dialog switches to a folder (agent tool call)
    agent = UniversalDevAgent(workspace=None, target_file=None)
    agent.core._orchestrator_slug = "teacher_assistant"
    assert agent._current_orchestrator_slug() == "teacher_assistant"

    switch = agent.dispatch("set_workspace", {"path": str(proj)})
    assert switch.get("ok") is True

    # then: the teacher menu shows the folder...
    teacher = ToolExecutor()
    teacher._orchestrator_slug = "teacher_assistant"
    assert picked in _menu_paths(teacher)

    # ...and the dev_agent menu does not (scoped write)
    dev = ToolExecutor()
    assert picked not in _menu_paths(dev)
