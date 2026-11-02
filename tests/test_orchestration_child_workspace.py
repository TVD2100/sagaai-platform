# -*- coding: utf-8 -*-
"""tests/test_orchestration_child_workspace.py - the folder a child inherits.

A spawned child works in the project of the dialog that started it. The
folder travels through ``_delegation_base_dir`` into the run record and the
child's transcript meta, so a lost child stays traceable to its project.

These tests pin the resolution order and, above all, the empty state: a
dialog that has not selected a folder must NOT hand its child another
dialog's folder.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config  # noqa: E402
from dev_agent import workspace_binding as wb  # noqa: E402
from dev_agent.agent_loop import _delegation_base_dir  # noqa: E402


@pytest.fixture(autouse=True)
def clean_bindings():
    """Leave no binding behind between tests."""
    yield
    for tid in ("t_child_ws", "t_child_ws_empty"):
        wb.clear_thread(tid)


def test_bound_thread_folder_wins(monkeypatch):
    """Given a dialog bound to a project, the child gets that project."""
    wb.register_thread("t_child_ws", workspace="/tmp/project_alpha")
    # The live config points elsewhere; the binding must take precedence.
    monkeypatch.setattr(config, "PROJECT_ROOT", "/tmp/some_other_root")
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True)

    assert _delegation_base_dir("t_child_ws") == "/tmp/project_alpha"


def test_unbound_thread_falls_back_to_config_root(monkeypatch):
    """Given no binding, the child inherits the live selected root."""
    monkeypatch.setattr(config, "PROJECT_ROOT", "/tmp/project_beta")
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True)

    assert _delegation_base_dir("t_never_registered") == "/tmp/project_beta"


def test_empty_state_hands_no_folder(monkeypatch):
    """Given the neutral empty state, the child gets None, not a stray root."""
    monkeypatch.setattr(config, "PROJECT_ROOT", config.NEUTRAL_ROOT)
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)

    assert _delegation_base_dir("t_child_ws_empty") is None


def test_binding_is_not_overridden_by_empty_state(monkeypatch):
    """A bound dialog keeps its folder even when the live flag reads empty."""
    wb.register_thread("t_child_ws", workspace="/tmp/project_gamma")
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)

    assert _delegation_base_dir("t_child_ws") == "/tmp/project_gamma"


def test_no_phantom_workspace_root_attribute(monkeypatch):
    """The resolver must not depend on a config attribute that does not exist.

    The child folder used to be read from ``config.WORKSPACE_ROOT``, which the
    config module never defined - the lookup raised, the value stayed None and
    every child ran without a project. The attribute is gone for good.
    """
    assert not hasattr(config, "WORKSPACE_ROOT")
    monkeypatch.setattr(config, "PROJECT_ROOT", "/tmp/project_delta")
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True)

    # Removing a non-existent attribute must not change the answer.
    assert _delegation_base_dir("") == "/tmp/project_delta"
