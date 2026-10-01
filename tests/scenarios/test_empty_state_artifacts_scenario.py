# -*- coding: utf-8 -*-
"""tests/scenarios/test_empty_state_artifacts_scenario.py - user-level
scenarios for dialog artifacts when NO workspace is selected (isolation v2).

Scenarios (given -> when -> then), walking the public entry points a user
actually triggers (the orchestrator reset helper, the chat send path of
``ui.pages.orchestrator.page_orchestrator`` and the journal scaffolding of
the agent loop):

  Scenario 1 - a dialog created in the empty state keeps its task-state
               journal inside its OWN dialog folder
               (history/<tid>/task_states) and creates nothing inside a
               neighbouring dialog's project.
  Scenario 2 - an attachment sent from the empty-state dialog lands in
               history/<tid>/files and never creates files in the
               neighbour's project folder.
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
NEIGHBOUR_TID = "scen_artifacts_neighbour"


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Temporary DATA_DIR that isolates DB and history from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_artifacts_")
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


def test_scenario_empty_state_journal_stays_in_dialog_folder(st_mock, tmp_path, monkeypatch):
    """Scenario 1: the empty-state journal lives in the dialog folder.

    Given a neighbouring dialog bound to a real project,
    when  a new dialog is opened, its first message is sent and the agent
          loop's journal scaffolding runs for it,
    then  the journal is stored under history/<tid>/task_states (the dialog
          owns its external memory) and the neighbour's project directory
          receives no new files.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from dev_agent import task_state as ts
    from dev_agent.universal_agent import UniversalDevAgent
    from core.paths import get_thread_dir
    import ui.pages.orchestrator as orch_page

    proj = _make_neighbour_project(tmp_path)
    before = sorted(os.listdir(proj))
    config.apply_paths(str(proj), create_dirs=False)
    wb.register_thread(NEIGHBOUR_TID, workspace=str(proj), target_file=None)

    monkeypatch.setattr(orch_page, "_do_step", lambda **kwargs: None)

    # when: a new dialog is opened and the first message is sent
    orch_page._reset_dialog(SLUG)
    _render_page(st_mock, monkeypatch, user_input="hello from the empty state")
    tid = st_mock.session_state.get(f"orch_{SLUG}_thread_id")
    assert tid, "the send path did not create a thread"

    # ...and the agent loop's journal scaffolding runs for this dialog
    dispatcher = UniversalDevAgent(workspace=None, target_file=None)
    dispatcher.set_thread_id(tid)
    assert wb.ensure_thread_active(tid) is True
    res = ts.ensure_task_state_file()

    # then: the journal lives in the dialog folder, never in the neighbour
    expected = Path(get_thread_dir(tid)) / "task_states" / f"TASK_STATE__{tid}.md"
    assert Path(res["path"]) == expected
    assert expected.exists()
    assert config.WORKSPACE_SELECTED is False
    assert sorted(os.listdir(proj)) == before


def test_scenario_empty_state_attachment_never_touches_foreign_project(st_mock, tmp_path, monkeypatch):
    """Scenario 2: empty-state attachments stay in the dialog folder.

    Given a neighbouring dialog bound to a real project,
    when  the user attaches a file in a new (empty-state) dialog and sends
          the message,
    then  the upload lands under history/<tid>/files and the neighbour's
          project directory receives no new files.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.paths import get_thread_dir
    import ui.pages.orchestrator as orch_page

    proj = _make_neighbour_project(tmp_path)
    before = sorted(os.listdir(proj))
    config.apply_paths(str(proj), create_dirs=False)
    wb.register_thread(NEIGHBOUR_TID, workspace=str(proj), target_file=None)

    monkeypatch.setattr(orch_page, "_do_step", lambda **kwargs: None)

    # when: a queued attachment is sent from the empty-state dialog
    orch_page._reset_dialog(SLUG)
    st_mock.session_state[f"orch_{SLUG}_attached"] = [
        {"name": "note.txt", "data": b"payload", "bytes": 7},
    ]
    _render_page(st_mock, monkeypatch, user_input="attach please")
    tid = st_mock.session_state.get(f"orch_{SLUG}_thread_id")
    assert tid, "the send path did not create a thread"

    # then: the upload is stored in the dialog's own files folder
    saved = os.path.join(get_thread_dir(tid), "files", "note.txt")
    assert os.path.isfile(saved)
    with open(saved, "rb") as fh:
        assert fh.read() == b"payload"

    # and the neighbour's project directory never received a file
    assert sorted(os.listdir(proj)) == before
