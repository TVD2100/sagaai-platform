# -*- coding: utf-8 -*-
"""tests/scenarios/test_thread_workspace_meta_scenario.py - user-level
scenarios for the thread "last folder" field (isolation v2).

Scenarios (given -> when -> then), walking the entry points the platform
actually uses: the shared thread loader ``ui.pages.orchestrator._load_thread``
(history page, deep links, theme-reload restore), the meta persist helper
``_persist_thread_workspace_meta`` (called from every agent step), the
DevAgent dispatcher ``UniversalDevAgent`` and ``core.threads_devagent``:

  Scenario 1 - reopening a saved dialog restores its project folder and
               mirrors it into the thread binding.
  Scenario 2 - a dialog whose folder disappeared opens in the EMPTY state
               (no folder selected); the stale folder is dropped from the
               thread meta on the next persist.
  Scenario 3 - a workspace-less dialog never records a neighbour's folder
               into its meta, while the neighbour keeps its own folder.
  Scenario 4 - deleting a dialog forgets its workspace binding, so the
               folder can never leak to other dialogs.
  Scenario 5 - a dispatcher workspace switch drops a stale single-file
               target, and the persist clears the meta in the empty state.
  Scenario 6 - a legacy foreign dialog with the platform folder as its
               saved workspace loses it in the startup migration and
               reopens in the EMPTY state.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine  # noqa: E402
from tests._st_mock import install_streamlit_mock  # noqa: E402

SLUG = "dev_agent"


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Temporary DATA_DIR that isolates DB and history from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_meta_")
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


@pytest.fixture()
def st_mock():
    """Fresh ui.* modules re-imported under the Streamlit mock."""
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _make_project(tmp_path, name):
    proj = tmp_path / name
    proj.mkdir()
    (proj / "f.txt").write_text("data", encoding="utf-8")
    return proj


def test_scenario_reopen_restores_saved_folder(st_mock, tmp_path):
    """Scenario 1: reopening a saved dialog restores its folder."""
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import create_devagent_thread
    import ui.pages.orchestrator as orch_page

    proj = _make_project(tmp_path, "saved_proj")
    tid = create_devagent_thread(
        title="saved dialog", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(proj), target_file=None,
    )

    # given: the live config currently belongs to a neighbouring dialog
    other = _make_project(tmp_path, "other_proj")
    config.apply_paths(str(other), create_dirs=False)

    # when: the saved dialog is reopened (history page / deep-link path)
    orch_page._init_orch_state(SLUG)
    orch_page._load_thread(SLUG, tid)

    # then: the dialog's OWN folder is restored and bound
    assert config.WORKSPACE_SELECTED is True
    assert str(config.PROJECT_ROOT) == str(proj)
    assert wb.get_thread_state(tid) == {"workspace": str(proj), "target_file": None}
    assert orch_page._ss(SLUG, "thread_id") == tid


def test_scenario_missing_folder_opens_empty_state(st_mock, tmp_path):
    """Scenario 2: a vanished folder opens the dialog in the empty state."""
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import create_devagent_thread, load_thread_meta
    import ui.pages.orchestrator as orch_page

    vanished = _make_project(tmp_path, "vanished_proj")
    tid = create_devagent_thread(
        title="stale dialog", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(vanished), target_file=None,
    )

    # given: the saved folder no longer exists
    config.apply_paths(str(vanished), create_dirs=False)
    shutil.rmtree(vanished)

    # when: the dialog is reopened
    orch_page._init_orch_state(SLUG)
    orch_page._load_thread(SLUG, tid)

    # then: it opens in the EMPTY state with a workspace-less binding
    assert config.WORKSPACE_SELECTED is False
    assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)
    assert wb.get_thread_state(tid) == {"workspace": None, "target_file": None}

    # and the next agent-step persist drops the stale folder from the meta
    orch_page._persist_thread_workspace_meta(tid)
    meta = load_thread_meta(tid)
    assert not meta.get("workspace")
    assert not meta.get("target_file")


def test_scenario_workspaceless_meta_not_polluted_by_neighbour(st_mock, tmp_path):
    """Scenario 3: a folder-less dialog never records a neighbour's folder."""
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import create_devagent_thread, load_thread_meta
    import ui.pages.orchestrator as orch_page

    proj = _make_project(tmp_path, "neighbour_proj")
    config.apply_paths(str(proj), create_dirs=False)

    nb_tid = create_devagent_thread(
        title="neighbour", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(proj), target_file=None,
    )
    wb.register_thread(nb_tid, workspace=str(proj), target_file=None)

    new_tid = create_devagent_thread(
        title="new dialog", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=None, target_file=None,
    )
    wb.register_thread(new_tid, workspace=None, target_file=None)

    # when: the step persist runs while the live config holds the neighbour
    orch_page._persist_thread_workspace_meta(new_tid)
    orch_page._persist_thread_workspace_meta(nb_tid)

    # then: the workspace-less dialog stays folder-less...
    new_meta = load_thread_meta(new_tid)
    assert not new_meta.get("workspace")
    assert not new_meta.get("target_file")
    # ...while the neighbour keeps its own folder.
    nb_meta = load_thread_meta(nb_tid)
    assert nb_meta.get("workspace") == str(proj)


def test_scenario_delete_thread_drops_binding(tmp_path):
    """Scenario 4: deleting a dialog forgets its workspace binding."""
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import (
        create_devagent_thread, delete_thread, load_thread_meta,
    )

    proj = _make_project(tmp_path, "deleted_proj")
    tid = create_devagent_thread(
        title="to delete", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(proj), target_file=None,
    )
    wb.register_thread(tid, workspace=str(proj), target_file=None)
    assert wb.has_thread(tid) is True

    delete_thread(tid)

    assert wb.has_thread(tid) is False
    assert not (load_thread_meta(tid) or {}).get("workspace")


def test_scenario_dispatcher_switch_and_empty_state_persist(tmp_path):
    """Scenario 5: switch drops a stale target; empty state clears the meta."""
    from dev_agent import config
    from dev_agent.universal_agent import UniversalDevAgent
    from core.threads_devagent import create_devagent_thread, load_thread_meta

    proj = _make_project(tmp_path, "switch_proj")
    target = proj / "f.txt"

    tid = create_devagent_thread(
        title="switch dialog", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(proj), target_file=str(target),
    )
    config.apply_paths(str(proj), target_file=str(target), create_dirs=False)
    agent = UniversalDevAgent(workspace=None, target_file=None)
    agent.set_thread_id(tid)

    # when: the dialog switches back to whole-folder mode
    switch = agent.dispatch("set_workspace", {"path": str(proj)})
    assert switch.get("ok") is True

    # then: the stale single-file target is dropped from the meta
    meta = load_thread_meta(tid)
    assert meta.get("workspace") == str(proj)
    assert meta.get("target_file") is None

    # when: the persist runs in the EMPTY state (no folder selected)
    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False, selected=False)
    agent._persist_thread_workspace()

    # then: the folder fields are cleared - the neutral root is never stored
    meta2 = load_thread_meta(tid)
    assert not meta2.get("workspace")
    assert not meta2.get("target_file")


def test_scenario_legacy_platform_folder_migrated_before_reopen(st_mock, tmp_path):
    """Scenario 6: legacy foreign dialogs lose leaked platform folders.

    given a teacher dialog left over from older builds with the SagaAI
    install root as its saved workspace, when the app-start migration runs
    and the dialog is reopened from history, then it opens in the EMPTY
    state instead of switching the agent to the platform folder.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import (
        create_devagent_thread, migrate_platform_workspace_meta,
    )
    import ui.pages.orchestrator as orch_page

    install = str(config.INSTALL_ROOT.resolve())
    tid = create_devagent_thread(
        title="legacy teacher dialog", orchestrator_slug="teacher_assistant",
        orchestrator_name="Teacher", workspace=install, target_file=None,
    )

    # when: the app-start migration runs ...
    assert migrate_platform_workspace_meta() == 1

    # ...and the dialog is reopened from history
    orch_page._init_orch_state("teacher_assistant")
    orch_page._load_thread("teacher_assistant", tid)

    # then: it opens in the EMPTY state, not on the platform folder
    assert config.WORKSPACE_SELECTED is False
    assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)
    assert wb.get_thread_state(tid) == {"workspace": None, "target_file": None}