# -*- coding: utf-8 -*-
"""tests/scenarios/test_new_dialog_empty_workspace_scenario.py - user-level
scenarios for the EMPTY workspace state of new dialogs (isolation v2).

Scenarios (given -> when -> then), walking the public entry points a user
actually triggers: the orchestrator reset helper used by the sidebar, the
chat toolbar and the deep-link handlers, the chat send path of
``ui.pages.orchestrator.page_orchestrator`` and the DevAgent dispatcher
``dev_agent.universal_agent.UniversalDevAgent.dispatch``:

  Scenario 1 - opening a new dialog while a neighbouring dialog works on a
               real project switches the live state to the empty workspace
               ("no folder selected") and leaves the neighbour's binding
               untouched.
  Scenario 2 - the first message in a new dialog creates a thread with NO
               saved folder (workspace=None), the dialog's fresh dispatcher
               registers a workspace-less binding, and the neighbour's
               project directory receives no new files.
  Scenario 3 - the new dialog's agent is blocked by the workspace guard,
               while the neighbour keeps reading its own project and files.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine  # noqa: E402
from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

SLUG = "dev_agent"
NEIGHBOUR_TID = "scen_neighbour_tid"


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Temporary DATA_DIR that isolates DB and history from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_new_dlg_")
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


def _make_neighbour_project(tmp_path):
    """Create a real project folder a neighbouring dialog is working on."""
    proj = tmp_path / "neighbour_proj"
    proj.mkdir()
    (proj / "nb.txt").write_text("neighbour-data", encoding="utf-8")
    return proj


def _render_page(st, monkeypatch, user_input=None):
    """Render the orchestrator page with a controllable chat input."""
    import ui.pages.orchestrator as orch_page

    orch = {
        "slug": SLUG, "name": "DevAgent", "description": "",
        "is_builtin": True, "prompt_text": "",
        "config": {"strong_service": "Mock", "strong_model": "m1"},
    }
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch if s == SLUG else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)

    def _input(*args, **kwargs):
        st._rec("chat_input", args, kwargs)
        return user_input

    monkeypatch.setattr(st, "chat_input", _input)
    st.session_state.update({"ui_lang": "English"})
    try:
        orch_page.page_orchestrator(SLUG)
    except StopRerun:
        pass
    return orch_page


def test_scenario_new_dialog_switches_to_empty_state(st_mock, tmp_path):
    """Scenario 1: opening a new dialog never inherits a folder.

    Given a neighbouring dialog bound to a real project (that project also
    sits in the process-global config, as right after the neighbour's last
    step),
    when  the user opens a new dialog (the same reset helper the sidebar,
          the toolbar and the deep-link handlers call),
    then  the live state becomes the empty workspace state and the
          neighbour's binding is untouched.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    import ui.pages.orchestrator as orch_page

    proj = _make_neighbour_project(tmp_path)
    config.apply_paths(str(proj), create_dirs=False)
    wb.register_thread(NEIGHBOUR_TID, workspace=str(proj), target_file=None)

    # when: a new dialog is opened
    orch_page._reset_dialog(SLUG)

    # then: the live state is the EMPTY workspace state
    assert config.WORKSPACE_SELECTED is False
    assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)

    # and the neighbour keeps its own binding
    state = wb.get_thread_state(NEIGHBOUR_TID)
    assert state is not None
    assert state["workspace"] == str(proj)


def test_scenario_first_message_creates_workspaceless_thread(st_mock, tmp_path, monkeypatch):
    """Scenario 2: a brand-new dialog's thread stores no folder.

    Given a neighbouring dialog bound to a real project,
    when  the sidebar opens a fresh dialog and the user sends the first
          message,
    then  the created thread carries NO saved folder, the dialog's fresh
          dispatcher registers a workspace-less binding, and the
          neighbour's project directory receives no new files.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from dev_agent.universal_agent import UniversalDevAgent
    from core.threads_devagent import load_thread_meta

    proj = _make_neighbour_project(tmp_path)
    before = sorted(os.listdir(proj))
    config.apply_paths(str(proj), create_dirs=False)
    wb.register_thread(NEIGHBOUR_TID, workspace=str(proj), target_file=None)

    import ui.pages.orchestrator as orch_page
    monkeypatch.setattr(orch_page, "_do_step", lambda **kwargs: None)

    # when: the user opens a new dialog and sends the first message
    orch_page._reset_dialog(SLUG)
    _render_page(st_mock, monkeypatch, user_input="hello, new dialog")

    # then: the thread exists and stores NO folder anywhere
    tid = st_mock.session_state.get(f"orch_{SLUG}_thread_id")
    assert tid, "the send path did not create a thread"
    meta = load_thread_meta(tid)
    assert not meta.get("workspace")
    assert not meta.get("target_file")

    # and the dialog's fresh dispatcher binds it as a workspace-less thread
    dispatcher = UniversalDevAgent(workspace=None, target_file=None)
    dispatcher.set_thread_id(tid)
    assert wb.get_thread_state(tid) == {"workspace": None, "target_file": None}

    # and the neighbour's project directory is untouched
    assert sorted(os.listdir(proj)) == before
    assert config.WORKSPACE_SELECTED is False


def test_scenario_new_dialog_agent_guarded_neighbour_reads_own_project(tmp_path):
    """Scenario 3: the empty dialog is guarded; the neighbour keeps working.

    Given a neighbour dialog bound to a real project and a brand-new dialog
    whose thread binding is workspace-less,
    when  the new dialog's agent tries to read a file, and the neighbour
          reads its own file afterwards,
    then  the new dialog gets the structured workspace_not_selected error
          and the neighbour still sees its own project and content.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from dev_agent.universal_agent import UniversalDevAgent

    proj = _make_neighbour_project(tmp_path)

    config.apply_paths(str(proj), create_dirs=False)
    neighbour = UniversalDevAgent(workspace=None, target_file=None)
    neighbour.set_thread_id(NEIGHBOUR_TID)

    # given: a new dialog - the reset switched the live state to the empty one
    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False, selected=False)
    new_dialog = UniversalDevAgent(workspace=None, target_file=None)
    new_dialog.set_thread_id("scen_new_tid")
    assert wb.get_thread_state("scen_new_tid") == {"workspace": None, "target_file": None}

    # when: the empty dialog's agent tries to touch a file
    blocked = new_dialog.dispatch("read_file", {"path": "nb.txt"})

    # then: it is guarded and cannot reach the neighbour's project
    assert blocked.get("ok") is False
    assert blocked.get("workspace_not_selected") is True
    assert blocked.get("suggestion")

    # and the neighbour still works inside its own project
    cur = neighbour.dispatch("current_workspace", {})
    assert cur["workspace_selected"] is True
    assert cur["root"] == str(proj)
    read = neighbour.dispatch("read_file", {"path": "nb.txt"})
    assert read.get("ok") is True
    assert "neighbour-data" in read["content"]
