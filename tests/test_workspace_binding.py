# -*- coding: utf-8 -*-
"""tests/test_workspace_binding.py - unit tests for the thread isolation
layer ``dev_agent.workspace_binding``.

Covers:
  * registry: register/get/clear/has for thread ids (normalization included);
  * ``sync_registry_from_config`` mirrors the live config state;
  * ``thread_context`` applies a bound state for the duration of one block
    and restores the previous global state afterwards;
  * an unbound thread id runs the block WITHOUT swapping the config;
  * ``ensure_thread_active`` keeps the applied state after the call (agent
    loop path), while returning False for unknown/empty ids;
  * ``set_target_root`` never mutates ``os.environ`` (no leakage of the last
    switched workspace into subprocesses of parallel dialogs);
  * concurrent ``thread_context`` blocks with different threads always
    observe their own project root (serialized by the binding RLock).
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
def clean_config_state():
    """Save/restore the process-global config and the binding registry so every
    test starts from a pristine state (this test file is the only writer of
    these globals)."""
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
    return str(p)


# ─── registry ──────────────────────────────────────────────────────────────

def test_register_and_get_thread_state(tmp_path):
    ws = _make_ws(tmp_path, "p")

    wb.register_thread("tid-a", workspace=ws, target_file=None)
    assert wb.has_thread("tid-a") is True
    assert wb.get_thread_workspace("tid-a") == ws
    assert wb.get_thread_state("tid-a") == {
        "workspace": ws,
        "target_file": None,
    }

    # Normalization: whitespace around the id is stripped.
    assert wb.get_thread_workspace("  tid-a ") == ws


def test_register_empty_id_is_noop():
    assert wb.register_thread("", workspace="/x") == {
        "workspace": None,
        "target_file": None,
    }
    assert wb.get_thread_state("") is None
    assert wb.get_thread_state("   ") is None


def test_clear_thread_forgets_binding(tmp_path):
    ws = _make_ws(tmp_path, "p")
    wb.register_thread("tid-c", workspace=ws)
    assert wb.has_thread("tid-c") is True

    wb.clear_thread("tid-c")
    assert wb.has_thread("tid-c") is False
    assert wb.get_thread_workspace("tid-c") is None


def test_sync_registry_from_config_captures_live_state(tmp_path):
    ws = _make_ws(tmp_path, "proj")
    target = os.path.join(ws, "main.py")
    Path(target).write_text("print('hi')", encoding="utf-8")

    config.apply_paths(ws, target_file=target, thread_id="tid-s", create_dirs=False)
    result = wb.sync_registry_from_config("tid-s")

    assert result["workspace"] == str(config.PROJECT_ROOT.resolve())
    assert result["target_file"] == target
    state = wb.get_thread_state("tid-s")
    assert state["workspace"] == str(config.PROJECT_ROOT)
    assert state["target_file"] == target


def test_sync_registry_empty_id_is_noop():
    assert wb.sync_registry_from_config("") == {
        "workspace": None,
        "target_file": None,
    }
    assert not wb.has_thread("")


# ─── thread_context ────────────────────────────────────────────────────────

def test_thread_context_applies_and_restores(tmp_path):
    ws_a = _make_ws(tmp_path, "A")
    ws_b = _make_ws(tmp_path, "B")

    config.set_target_root(ws_b)
    wb.register_thread("tid-ctx", workspace=ws_a)

    with wb.thread_context("tid-ctx") as ctx:
        assert ctx.engaged is True
        assert str(config.PROJECT_ROOT) == ws_a

    # The previous global state is restored after the block.
    assert str(config.PROJECT_ROOT) == ws_b


def test_thread_context_unbound_is_engaged_false_no_swap(tmp_path):
    ws_a = _make_ws(tmp_path, "A")
    config.set_target_root(ws_a)

    # No binding for this id: the block must run without touching config.
    with wb.thread_context("tid-none") as ctx:
        assert ctx.engaged is False
        assert str(config.PROJECT_ROOT) == ws_a
    assert str(config.PROJECT_ROOT) == ws_a


def test_thread_context_empty_id_no_swap(): 
    with wb.thread_context("") as ctx:
        assert ctx.engaged is False


def test_thread_context_restores_on_exception(tmp_path):
    ws_a = _make_ws(tmp_path, "A")
    ws_b = _make_ws(tmp_path, "B")
    config.set_target_root(ws_b)
    wb.register_thread("tid-exc", workspace=ws_a)

    with pytest.raises(RuntimeError):
        with wb.thread_context("tid-exc") as ctx:
            assert ctx.engaged is True
            raise RuntimeError("boom")

    assert str(config.PROJECT_ROOT) == ws_b


# ─── ensure_thread_active ──────────────────────────────────────────────────

def test_ensure_thread_active_applies_without_restore(tmp_path):
    ws_a = _make_ws(tmp_path, "A")
    ws_b = _make_ws(tmp_path, "B")
    config.set_target_root(ws_b)
    wb.register_thread("tid-ens", workspace=ws_a)

    assert wb.ensure_thread_active("tid-ens") is True
    # Unlike thread_context, the applied state STAYS (agent loop path).
    assert str(config.PROJECT_ROOT) == ws_a
    assert config.ACTIVE_THREAD_ID == "tid-ens"


def test_ensure_thread_active_unknown_returns_false(tmp_path):
    ws = _make_ws(tmp_path, "A")
    config.set_target_root(ws)
    config.ACTIVE_THREAD_ID = ""

    assert wb.ensure_thread_active("tid-unknown") is False
    assert str(config.PROJECT_ROOT) == ws


def test_ensure_thread_active_empty_id_returns_false():
    assert wb.ensure_thread_active("") is False


def test_ensure_thread_active_workspaceless_pins_thread_id(tmp_path):
    """A bound, workspace-less state keeps the current root but pins the
    thread id so journals still target the right thread."""
    ws = _make_ws(tmp_path, "A")
    config.set_target_root(ws)
    wb.register_thread("tid-less", workspace=None, target_file=None)

    assert wb.ensure_thread_active("tid-less") is True
    assert str(config.PROJECT_ROOT) == ws
    assert config.ACTIVE_THREAD_ID == "tid-less"


# ─── set_target_root / os.environ hygiene ──────────────────────────────────

def test_set_target_root_does_not_mutate_os_environ(tmp_path, monkeypatch):
    ws = _make_ws(tmp_path, "proj")
    env_before = dict(os.environ)

    config.set_target_root(ws)

    assert dict(os.environ) == env_before

    # Reset path also stays out of the environment.
    config.set_target_root(None)
    assert dict(os.environ) == env_before


# ─── concurrency: two threads never see each other's roots ─────────────────

def test_thread_context_parallel_isolation(tmp_path):
    """Two interleaved thread_context blocks always observe their OWN root,
    even when entered concurrently (serialized by the binding RLock)."""
    ws_a = _make_ws(tmp_path, "A")
    ws_b = _make_ws(tmp_path, "B")
    config.set_target_root(_make_ws(tmp_path, "DEFAULT"))
    wb.register_thread("tid-isoa", workspace=ws_a)
    wb.register_thread("tid-isob", workspace=ws_b)

    seen = {}

    def worker(tid):
        for _ in range(20):
            with wb.thread_context(tid) as ctx:
                assert ctx.engaged is True
                seen.setdefault(tid, set()).add(str(config.PROJECT_ROOT))

    import threading
    t1 = threading.Thread(target=worker, args=("tid-isoa",))
    t2 = threading.Thread(target=worker, args=("tid-isob",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive()
    assert not t2.is_alive()

    assert seen["tid-isoa"] == {ws_a}
    assert seen["tid-isob"] == {ws_b}
