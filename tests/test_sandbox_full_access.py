# -*- coding: utf-8 -*-
"""The full-access grant of the execution environment.

An employee the owner granted full access to must be able to run a task that
manages the machine: read the provider credentials the platform itself uses,
see the real home directory, call the network. The grant is a single switch
and it is read here as one argument of the environment builder, so there is
one place that decides what a child may see.

The isolated form stays the default: a caller that says nothing grants
nothing, which is what keeps a forgotten argument from widening access.
"""
from __future__ import annotations

import os

from dev_agent.sandbox_env import AUTO_HOME_PREFIX, build_child_env, discard_env_home


def test_full_access_exposes_the_platform_environment(monkeypatch):
    """A granted run receives the platform variables with their values."""
    monkeypatch.setenv("SAGAAI_YANDEXAI_KEY", "secret-value")
    env = build_child_env(project_root=None, isolate_env=False)
    assert env["SAGAAI_YANDEXAI_KEY"] == "secret-value"


def test_isolated_run_hides_the_platform_environment(monkeypatch):
    """The default keeps the same variable out of the child."""
    monkeypatch.setenv("SAGAAI_YANDEXAI_KEY", "secret-value")
    env = build_child_env(project_root=None)
    assert "SAGAAI_YANDEXAI_KEY" not in env
    assert all("secret-value" not in str(value) for value in env.values())


def test_full_access_keeps_the_real_home(monkeypatch, tmp_path):
    """The child sees the owner's home, not a throwaway directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    env = build_child_env(project_root=None, isolate_env=False)
    assert env["HOME"] == str(tmp_path)
    assert not os.path.basename(env["HOME"]).startswith(AUTO_HOME_PREFIX)


def test_full_access_home_is_never_removed(monkeypatch, tmp_path):
    """Cleanup removes only a directory this module created itself."""
    monkeypatch.setenv("HOME", str(tmp_path))
    env = build_child_env(project_root=None, isolate_env=False)
    assert discard_env_home(env) is False
    assert tmp_path.exists()


def test_full_access_puts_the_project_first_on_pythonpath(monkeypatch, tmp_path):
    """The project wins over an inherited path entry, and both survive."""
    inherited = str(tmp_path / "elsewhere")
    monkeypatch.setenv("PYTHONPATH", inherited)
    env = build_child_env(project_root=tmp_path, isolate_env=False)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == str(tmp_path)
    assert inherited in entries


def test_full_access_does_not_duplicate_the_project_entry(monkeypatch, tmp_path):
    """A project already on the inherited path appears once, in front."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), "/opt/lib"]))
    env = build_child_env(project_root=tmp_path, isolate_env=False)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries.count(str(tmp_path)) == 1
    assert entries[0] == str(tmp_path)


def test_full_access_still_forwards_the_enclosing_sandbox(monkeypatch):
    """A nested run must still see that it already executes confined."""
    monkeypatch.setenv("SAGAAI_SANDBOX_GRANTS", "v1;isolation=os;network=0;writable=1;profile=workspace_write")
    env = build_child_env(project_root=None, isolate_env=False)
    assert env["SAGAAI_SANDBOX_GRANTS"].startswith("v1;")

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
