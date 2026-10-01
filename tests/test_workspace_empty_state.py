# -*- coding: utf-8 -*-
"""tests/test_workspace_empty_state.py - unit tests for the EMPTY workspace
state ("no folder selected") introduced by the workspace-isolation v2 work.

Covers:
  * ``config.apply_paths(selected=False)`` switches PROJECT_ROOT to the
    neutral root and clears WORKSPACE_SELECTED;
  * the selected flag is derived from the root when not passed explicitly;
  * ``config.snapshot_state``/``restore_state`` round-trip the flag;
  * registering the neutral root as a workspace stores a workspace-less
    binding (the neutral root is never treated as a chosen folder);
  * a bound, workspace-less thread applies the neutral state during
    ``thread_context`` and restores the previous global root afterwards;
  * ``ensure_thread_active`` for a workspace-less thread never inherits a
    neighbouring dialog's root (v2 semantics);
  * ``workspace_tools.current_workspace`` reports ``workspace_selected``
    and a Stage-0 hint in the empty state.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config  # noqa: E402
from dev_agent import workspace_binding as wb  # noqa: E402
from dev_agent import workspace_tools as wt  # noqa: E402


@pytest.fixture(autouse=True)
def clean_config_state():
    """Save/restore the process-global config and the binding registry so
    every test starts from a pristine state."""
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


# ─── config: neutral root + selected flag ─────────────────────────────────

def test_apply_paths_selected_false_switches_to_neutral(tmp_path):
    config.apply_paths(_make_ws(tmp_path, "proj"), create_dirs=False)
    assert config.WORKSPACE_SELECTED is True

    config.apply_paths(str(config.NEUTRAL_ROOT),
                       create_dirs=False, selected=False)
    assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)
    assert config.WORKSPACE_SELECTED is False


def test_apply_paths_derives_selected_flag_from_root(tmp_path):
    config.apply_paths(_make_ws(tmp_path, "proj"), create_dirs=False)
    assert config.WORKSPACE_SELECTED is True

    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False)
    assert config.WORKSPACE_SELECTED is False


def test_snapshot_restore_round_trips_selected_flag(tmp_path):
    config.apply_paths(_make_ws(tmp_path, "proj"), create_dirs=False)
    snap = config.snapshot_state()
    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False, selected=False)
    assert config.WORKSPACE_SELECTED is False

    config.restore_state(snap)
    assert config.WORKSPACE_SELECTED is True


# ─── registry: neutral root is never a chosen workspace ────────────────────

def test_register_neutral_root_stores_workspaceless_binding():
    wb.register_thread("tid-n", workspace=str(config.NEUTRAL_ROOT))
    assert wb.get_thread_state("tid-n") == {
        "workspace": None,
        "target_file": None,
    }


# ─── thread context / ensure_thread_active ─────────────────────────────────

def test_workspaceless_thread_context_applies_neutral_and_restores(tmp_path):
    ws = _make_ws(tmp_path, "A")
    config.set_target_root(ws)
    wb.register_thread("tid-empty", workspace=None)

    with wb.thread_context("tid-empty") as ctx:
        assert ctx.engaged is True
        assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)
        assert config.WORKSPACE_SELECTED is False

    # Previous global state is restored after the block: neighbours untouched.
    assert str(config.PROJECT_ROOT) == ws
    assert config.WORKSPACE_SELECTED is True


def test_ensure_thread_active_workspaceless_no_inheritance(tmp_path):
    ws = _make_ws(tmp_path, "A")
    config.set_target_root(ws)
    wb.register_thread("tid-empty2", workspace=None)

    assert wb.ensure_thread_active("tid-empty2") is True
    assert str(config.PROJECT_ROOT) == str(config.NEUTRAL_ROOT)
    assert config.WORKSPACE_SELECTED is False
    assert config.ACTIVE_THREAD_ID == "tid-empty2"


# ─── current_workspace reporting ───────────────────────────────────────────

def test_current_workspace_reports_empty_state():
    config.apply_paths(config.NEUTRAL_ROOT, create_dirs=False, selected=False)
    res = wt.current_workspace()
    assert res["ok"] is True
    assert res["workspace_selected"] is False
    assert "hint" in res


def test_current_workspace_reports_selected_root(tmp_path):
    ws = _make_ws(tmp_path, "proj")
    config.apply_paths(ws, create_dirs=False)
    res = wt.current_workspace()
    assert res["workspace_selected"] is True
    assert "hint" not in res
