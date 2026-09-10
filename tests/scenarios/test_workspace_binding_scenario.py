# -*- coding: utf-8 -*-
"""tests/scenarios/test_workspace_binding_scenario.py - user-level scenario
 tests for per-thread workspace isolation of parallel DevAgent dialogs.

Scenarios (given -> when -> then), walking the public DevAgent entry point
``dev_agent.universal_agent.UniversalDevAgent.dispatch`` - exactly the way
 the chat loop calls these tools:

  Scenario 1 - parallel dialogs stay isolated: two bound dispatchers work on
               different projects in interleaved order; switching the
               workspace in dialog A repoints ONLY A - B keeps seeing its
               own files and root, and A's reads go to the NEW project.
  Scenario 2 - a switched workspace survives "restart": after A switches,
               the new state is persisted into the thread DB meta; a fresh
               dispatcher bound to the same thread (as after reopening the
               saved dialog) sees the NEW workspace.
  Scenario 3 - legacy fallback: a dispatcher without a thread id keeps the
               process-global behavior (whatever config.PROJECT_ROOT holds)
               and never piggybacks on another thread's binding.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dev_agent import config  # noqa: E402
from dev_agent import workspace_binding as wb  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point SagaAI at a fresh temp data dir and isolate module caches."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield tmp_path

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


@pytest.fixture(autouse=True)
def clean_binding_state():
    """Save/restore config globals and the registry so scenarios run clean."""
    state = config.snapshot_state()
    with wb.sync_lock():
        old_registry = dict(wb._REGISTRY)
        wb._REGISTRY.clear()
    yield
    config.restore_state(state)
    with wb.sync_lock():
        wb._REGISTRY.clear()
        wb._REGISTRY.update(old_registry)


def _make_ws(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    return p


def test_scenario_parallel_dialogs_stay_isolated(tmp_path):
    """Scenario 1: two dialogs never see each other's workspace.

    Given two projects sharing a file name and two dispatchers bound to
    different threads, when they read files in interleaved order and dialog
    A switches its workspace, then A sees the new project while B keeps its
    own root and file contents.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    ws_a = _make_ws(tmp_path, "proj_a")
    ws_b = _make_ws(tmp_path, "proj_b")
    (ws_a / "shared_name.txt").write_text("A-version", encoding="utf-8")
    (ws_b / "shared_name.txt").write_text("B-version", encoding="utf-8")

    # given: dispatcher A bound to thread-a on proj_a, B to thread-b on proj_b
    config.set_target_root(str(ws_a))
    agent_a = UniversalDevAgent(workspace=None, target_file=None)
    agent_a.set_thread_id("thread-a")
    config.set_target_root(str(ws_b))
    agent_b = UniversalDevAgent(workspace=None, target_file=None)
    agent_b.set_thread_id("thread-b")

    # when: interleaved reads (the worst-case interleaving happens naturally
    # because each dispatch swaps the globals for its own thread only)
    read_a = agent_a.dispatch("read_file", {"path": "shared_name.txt"})
    read_b = agent_b.dispatch("read_file", {"path": "shared_name.txt"})
    cur_a = agent_a.dispatch("current_workspace", {})
    cur_b = agent_b.dispatch("current_workspace", {})

    # then: each dialog observes its own project
    assert read_a["ok"] is True
    assert "A-version" in read_a["content"]
    assert read_b["ok"] is True
    assert "B-version" in read_b["content"]
    assert cur_a["root"] == str(ws_a.resolve())
    assert cur_b["root"] == str(ws_b.resolve())

    # when: dialog A switches to a third project
    ws_c = _make_ws(tmp_path, "proj_c")
    switch = agent_a.dispatch("set_workspace", {"path": str(ws_c)})
    assert switch["ok"] is True

    # then: A is repointed, B stays on its own project
    cur_a2 = agent_a.dispatch("current_workspace", {})
    cur_b2 = agent_b.dispatch("current_workspace", {})
    assert cur_a2["root"] == str(ws_c.resolve())
    assert cur_b2["root"] == str(ws_b.resolve())

    read_b2 = agent_b.dispatch("read_file", {"path": "shared_name.txt"})
    assert read_b2["ok"] is True
    assert "B-version" in read_b2["content"]

    # A now works inside the new project: it no longer sees proj_a's file.
    (ws_c / "c.txt").write_text("C-version", encoding="utf-8")
    read_c = agent_a.dispatch("read_file", {"path": "c.txt"})
    assert read_c["ok"] is True
    assert "C-version" in read_c["content"]
    read_a_gone = agent_a.dispatch("read_file", {"path": "shared_name.txt"})
    assert read_a_gone["ok"] is False


def test_scenario_switched_workspace_survives_restart(tmp_path):
    """Scenario 2: reopening a saved dialog restores the LAST workspace.

    Given dialog A bound to a DB thread and switched to a new project, when
    the dialog is "reopened" (fresh dispatcher + the UI restore path), then
    the fresh dispatcher sees the switched workspace and reads its files.
    """
    from dev_agent import workspace_tools as wt
    from dev_agent.universal_agent import UniversalDevAgent
    from core.threads_devagent import create_devagent_thread, load_thread_meta

    ws_a = _make_ws(tmp_path, "proj_a")
    ws_b = _make_ws(tmp_path, "proj_b")
    (ws_b / "b.txt").write_text("B-data", encoding="utf-8")

    config.set_target_root(str(ws_a))
    tid = create_devagent_thread(
        title="parallel dialog",
        orchestrator_slug="dev_agent",
        orchestrator_name="DevAgent",
        workspace=str(ws_a),
        target_file=None,
    )
    agent = UniversalDevAgent(workspace=None, target_file=None)
    agent.set_thread_id(tid)

    switch = agent.dispatch("set_workspace", {"path": str(ws_b)})
    assert switch["ok"] is True

    # given: the switch was persisted into the thread DB meta
    meta = load_thread_meta(tid)
    assert meta["workspace"] == str(ws_b.resolve())

    # when: a fresh dispatcher bound to the SAME thread (dialog reopened),
    # mirroring exactly what the UI does: restore the saved workspace into
    # config, then sync the registry entry from the live config
    fresh = UniversalDevAgent(workspace=None, target_file=None)
    fresh.set_thread_id(tid)
    wt.set_workspace(meta["workspace"])
    wb.sync_registry_from_config(tid)

    # then: the fresh dispatcher sees the switched workspace
    cur = fresh.dispatch("current_workspace", {})
    assert cur["root"] == str(ws_b.resolve())
    read_b = fresh.dispatch("read_file", {"path": "b.txt"})
    assert read_b["ok"] is True
    assert "B-data" in read_b["content"]


def test_scenario_unbound_dispatcher_legacy_fallback(tmp_path):
    """Scenario 3: an unbound dispatcher keeps legacy process-global mode.

    Given a workspace and a dispatcher WITHOUT a thread id, when tools are
    dispatched, then every result reflects the live config globals and no
    other thread's binding leaks in.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    ws = _make_ws(tmp_path, "proj")
    (ws / "f.txt").write_text("X-content", encoding="utf-8")
    config.set_target_root(str(ws))

    agent = UniversalDevAgent(workspace=None, target_file=None)
    assert agent.thread_id is None

    cur = agent.dispatch("current_workspace", {})
    assert cur["root"] == str(ws.resolve())

    read_f = agent.dispatch("read_file", {"path": "f.txt"})
    assert read_f["ok"] is True
    assert "X-content" in read_f["content"]
