# -*- coding: utf-8 -*-
"""tests/scenarios/test_folder_picker_scenario.py - user-level scenarios for
the chat folder selector ("Выбор папки") and the automatic workspace switch.

The scenarios walk the real entry point - ``page_orchestrator`` with its
send branch - plus the native-dialog button, and assert the behaviour the
user sees:

  Scenario 1 - a folder picked in the selector is applied (set_workspace)
               automatically when the FIRST message is sent; the thread is
               bound to it and the selector snaps back to "Авто".
  Scenario 2 - a folder switched mid-dialog (X -> Y) is applied on the next
               send, and neither switch is repeated on later messages.
  Scenario 3 - a temporary detour the agent itself made (live config points
               at another folder) never changes what the UI reports: the
               dialog keeps ITS bound folder.
  Scenario 4 - the native "Обзор…" button records the picked folder in the
               dialog's recent list and stages it in the selector.
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


@pytest.fixture(autouse=True)
def isolated_data_dir():
    """Temporary DATA_DIR that isolates DB and history from real data."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_picker_")
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
    """Create a real project folder with one file."""
    proj = tmp_path / name
    proj.mkdir()
    (proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return proj


def _prepare_page(st, monkeypatch):
    """Load the page module under the mock and stub its collaborators."""
    import ui.pages.orchestrator as orch_page

    orch = {
        "slug": SLUG, "name": "DevAgent", "description": "",
        "is_builtin": True, "prompt_text": "",
        "config": {"strong_service": "Mock", "strong_model": "m1"},
    }
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch if s == SLUG else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    # The native dialog must never be spawned from a test render.
    monkeypatch.setattr(orch_page.folder_dialog, "native_dialog_available",
                        lambda *a, **k: False)
    monkeypatch.setattr(orch_page, "_do_step", lambda **kwargs: None)
    st.session_state.update({"ui_lang": "English"})
    return orch_page


def _render_page(st, orch_page, monkeypatch, user_input=None):
    """Render the orchestrator page with a controllable chat input."""
    def _input(*args, **kwargs):
        st._rec("chat_input", args, kwargs)
        return user_input

    monkeypatch.setattr(st, "chat_input", _input)
    try:
        orch_page.page_orchestrator(SLUG)
    except StopRerun:
        pass
    return orch_page


def test_scenario_picked_folder_applied_on_first_send(st_mock, tmp_path, monkeypatch):
    """Scenario 1: the selector's folder is applied by the send, once.

    Given a new dialog and a folder picked in the selector,
    when  the user sends the first message,
    then  set_workspace runs BEFORE the turn, the thread is bound to that
          folder, the selector snaps back to "Авто", and the second send
          does not call set_workspace again.
    """
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import load_thread_meta
    import ui.pages.orchestrator as orch_page

    proj = _make_project(tmp_path, "picked_proj")
    picked = str(proj.resolve())

    orch_page = _prepare_page(st_mock, monkeypatch)
    calls = []
    real_set_workspace = orch_page.wt.set_workspace

    def _spy(path, slug=None):
        calls.append((str(path), slug))
        return real_set_workspace(path, slug=slug)

    monkeypatch.setattr(orch_page.wt, "set_workspace", _spy)

    # given: the user picked a folder in the selector
    st_mock.session_state[f"orch_{SLUG}_folder_choice"] = picked

    # when: the first message is sent
    _render_page(st_mock, orch_page, monkeypatch, user_input="start the task")

    # then: the switch happened BEFORE the turn, in this orchestrator's scope
    assert calls == [(picked, SLUG)], calls
    tid = st_mock.session_state.get(f"orch_{SLUG}_thread_id")
    assert tid, "the send path did not create a thread"
    meta = load_thread_meta(tid)
    assert meta.get("workspace") == picked
    assert wb.get_thread_workspace(tid) == picked
    # and the selector went back to "Авто"
    assert st_mock.session_state[f"orch_{SLUG}_folder_choice"] == ""

    # when: a second message is sent (nothing staged any more)
    _render_page(st_mock, orch_page, monkeypatch, user_input="continue")

    # then: no further workspace switch was made
    assert len(calls) == 1, calls


def test_scenario_mid_dialog_switch_applies_new_folder(st_mock, tmp_path, monkeypatch):
    """Scenario 2: switching folders mid-dialog re-points the dialog.

    Given a dialog already working in project X,
    when  the user picks project Y in the selector and sends a message,
    then  set_workspace(Y) runs, the thread binding moves X -> Y and the
    selector returns to "Авто".
    """
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import create_devagent_thread, load_thread_meta
    import ui.pages.orchestrator as orch_page

    proj_x = _make_project(tmp_path, "proj_x")
    proj_y = _make_project(tmp_path, "proj_y")

    # given: an existing dialog bound to project X
    tid = create_devagent_thread(
        title="existing dialog", orchestrator_slug=SLUG,
        orchestrator_name="DevAgent", workspace=str(proj_x.resolve()),
    )
    wb.register_thread(tid, workspace=str(proj_x.resolve()), target_file=None)

    orch_page = _prepare_page(st_mock, monkeypatch)
    calls = []
    real_set_workspace = orch_page.wt.set_workspace
    monkeypatch.setattr(orch_page.wt, "set_workspace",
                        lambda p, slug=None: calls.append(str(p)) or real_set_workspace(p, slug=slug))

    st_mock.session_state[f"orch_{SLUG}_thread_id"] = tid
    st_mock.session_state[f"orch_{SLUG}_folder_choice"] = str(proj_y.resolve())

    # when: the next message is sent
    _render_page(st_mock, orch_page, monkeypatch, user_input="switch to the other project")

    # then: the dialog moved to Y and it is persisted
    assert calls == [str(proj_y.resolve())], calls
    assert wb.get_thread_workspace(tid) == str(proj_y.resolve())
    assert load_thread_meta(tid).get("workspace") == str(proj_y.resolve())
    assert st_mock.session_state[f"orch_{SLUG}_folder_choice"] == ""


def test_scenario_agent_detour_does_not_change_reported_folder(st_mock, tmp_path, monkeypatch):
    """Scenario 3: a temporary agent detour must not confuse the UI.

    Given a dialog bound to project X while the live config points at
    another folder (the agent is inspecting it on the user's request),
    when  the chat is rendered,
    then  the displayed working folder is X (the thread binding stays the
          single source of truth) and nothing is re-applied.
    """
    from dev_agent import config
    from dev_agent import workspace_binding as wb
    from core.threads_devagent import create_devagent_thread
    import ui.pages.orchestrator as orch_page

    proj_x = _make_project(tmp_path, "bound_proj")
    other = _make_project(tmp_path, "inspection_proj")

    tid = create_devagent_thread(
        title="dialog", orchestrator_slug=SLUG, orchestrator_name="DevAgent",
        workspace=str(proj_x.resolve()),
    )
    wb.register_thread(tid, workspace=str(proj_x.resolve()), target_file=None)
    # the agent is temporarily looking into another folder
    config.apply_paths(str(other), create_dirs=False)

    orch_page = _prepare_page(st_mock, monkeypatch)
    calls = []
    real_set_workspace = orch_page.wt.set_workspace
    monkeypatch.setattr(orch_page.wt, "set_workspace",
                        lambda p, slug=None: calls.append(str(p)) or real_set_workspace(p, slug=slug))

    st_mock.session_state[f"orch_{SLUG}_thread_id"] = tid

    # when / then: the page reports the DIALOG's folder, not the live detour
    assert orch_page._thread_folder(SLUG) == str(proj_x.resolve())
    _render_page(st_mock, orch_page, monkeypatch, user_input=None)

    captions = [c for c in st_mock.calls if c[0] == "caption"]
    shown = [c[1][0] for c in captions if c[1] and isinstance(c[1][0], str)]
    assert any(str(proj_x.resolve()) in text for text in shown), shown
    # and the render applied no workspace switch of its own
    assert calls == [], calls


def test_scenario_browse_button_records_and_stages_folder(st_mock, tmp_path, monkeypatch):
    """Scenario 4: the native dialog result is remembered and staged.

    Given a machine where the native folder dialog is available,
    when  the user clicks "Обзор…" and picks a folder,
    then  the folder lands in the dialog's recent list and is staged in the
          selector (a fresh widget key); no workspace switch happens yet.
    """
    import ui.pages.orchestrator as orch_page

    chosen = _make_project(tmp_path, "browsed")
    chosen_path = str(chosen.resolve())

    orch_page = _prepare_page(st_mock, monkeypatch)
    monkeypatch.setattr(orch_page.folder_dialog, "native_dialog_available",
                        lambda *a, **k: True)
    monkeypatch.setattr(orch_page.folder_dialog, "pick_folder",
                        lambda *a, **k: chosen_path)
    recorded = []
    monkeypatch.setattr(orch_page, "add_recent_workspace",
                        lambda path, slug=None: recorded.append((str(path), slug)))
    calls = []
    monkeypatch.setattr(orch_page.wt, "set_workspace",
                        lambda p, slug=None: calls.append(str(p)) or {"ok": True})

    counter_before = int(st_mock.session_state.get(f"orch_{SLUG}_folder_picker_counter") or 0)
    st_mock.click(f"orch_folder_browse_{SLUG}")

    # when: the page renders with a clicked "Обзор…" button
    _render_page(st_mock, orch_page, monkeypatch, user_input=None)

    # then: the folder is recorded in this orchestrator's history and staged
    assert recorded == [(chosen_path, SLUG)], recorded
    assert st_mock.session_state[f"orch_{SLUG}_folder_choice"] == chosen_path
    assert int(st_mock.session_state[f"orch_{SLUG}_folder_picker_counter"]) == counter_before + 1
    # and the workspace was NOT switched yet (that happens on the next send)
    assert calls == [], calls
