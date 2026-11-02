# -*- coding: utf-8 -*-
"""
Tests for the working-memory root of an agent (``memory:`` paths).

The employee's working-data folder lives in the installation, outside the
project. File tools reach it through a second allowed root: the ``memory:``
prefix or an absolute path inside that folder. Everything outside BOTH roots
stays rejected, and one employee can never read another employee's folder.
"""
import os

import pytest

import core.paths
from dev_agent import config
from dev_agent import tool_executor as te


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    """Isolate the project root and the central working-data root."""
    central = tmp_path / "agents_data"
    central.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(central))
    # The resolver reads a stored record when prompt/config are not passed.
    # These tests exercise the ROOT MECHANICS, so the lookup is neutralised:
    # no leftover record from another run may change the expected path.
    from core import orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "get_orchestrator", lambda slug: None, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", project.resolve())
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", True)
    monkeypatch.setattr(config, "MEMORY_ROOT", None)
    yield {"central": central, "project": project}
    monkeypatch.setattr(config, "MEMORY_ROOT", None)


# ─── set_memory_root ─────────────────────────────────────────────────────────


def test_set_memory_root_uses_the_central_folder(roots):
    root = config.set_memory_root("marketer", str(roots["project"]))
    assert root is not None
    assert str(root) == str(roots["central"] / "marketer")
    assert config.memory_root() == root


def test_set_memory_root_empty_slug_disables_it(roots):
    assert config.set_memory_root("", "") is None
    assert config.memory_root() is None


def test_set_memory_root_is_independent_of_the_project(roots):
    """Two projects resolve the same employee to the SAME memory folder."""
    first = config.set_memory_root("marketer", str(roots["project"]))
    other = roots["project"].parent / "another_project"
    other.mkdir()
    second = config.set_memory_root("marketer", str(other))
    assert first == second


def test_set_memory_root_honours_an_external_override(roots, tmp_path):
    """An absolute MEMORY_DIR value is used verbatim, not relocated."""
    external = tmp_path / "external_desk"
    external.mkdir()
    root = config.set_memory_root(
        "editor", str(roots["project"]))
    assert root is not None
    # Now point the employee at an external folder through its config.
    from core import orchestrators as orch
    from core import orchestrator_folders as of
    resolved = of.resolve_data_dir("editor", str(roots["project"]),
                                   prompt_text=f"MEMORY_DIR: {external}",
                                   config={})
    assert resolved["path"] == str(external)
    assert resolved["scope"] == "external"


# ─── memory: addressing ──────────────────────────────────────────────────────


def test_memory_prefix_resolves_inside_the_root(roots):
    config.set_memory_root("marketer", str(roots["project"]))
    target = config.resolve_in_project("memory:registry.md")
    assert str(target) == str(roots["central"] / "marketer" / "registry.md")


def test_memory_prefix_accepts_nested_paths(roots):
    config.set_memory_root("project_manager", str(roots["project"]))
    target = config.resolve_in_project("memory:projects/sagaai/project.md")
    assert str(target) == str(
        roots["central"] / "project_manager" / "projects" / "sagaai" / "project.md"
    )


def test_project_paths_still_work(roots):
    config.set_memory_root("marketer", str(roots["project"]))
    target = config.resolve_in_project("README.md")
    assert str(target) == str(roots["project"] / "README.md")


def test_absolute_path_inside_memory_is_accepted(roots):
    root = config.set_memory_root("marketer", str(roots["project"]))
    target = config.resolve_in_project(str(root / "journal.md"))
    assert str(target) == str(root / "journal.md")


def test_absolute_memory_path_reports_the_prefix(roots):
    root = config.set_memory_root("marketer", str(roots["project"]))
    rel = config.to_project_relative(str(root / "journal.md"))
    assert rel == "memory:journal.md"


# ─── Rejections ──────────────────────────────────────────────────────────────


def test_memory_prefix_without_a_root_is_rejected(roots):
    config.set_memory_root("", "")
    with pytest.raises(ValueError):
        config.resolve_in_project("memory:registry.md")


def test_memory_prefix_cannot_escape_the_root(roots):
    config.set_memory_root("marketer", str(roots["project"]))
    with pytest.raises(ValueError):
        config.resolve_in_project("memory:../../etc/passwd")


def test_another_employee_folder_is_not_reachable(roots):
    """A sibling employee's folder is outside THIS root and stays rejected."""
    config.set_memory_root("marketer", str(roots["project"]))
    sibling = roots["central"] / "researcher" / "secret.md"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text("# секрет", encoding="utf-8")
    with pytest.raises(ValueError):
        config.resolve_in_project(str(sibling))
    with pytest.raises(ValueError):
        config.resolve_in_project("memory:../researcher/secret.md")


def test_path_outside_both_roots_is_rejected(roots, tmp_path):
    config.set_memory_root("marketer", str(roots["project"]))
    outside = tmp_path / "elsewhere" / "file.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        config.resolve_in_project(str(outside))


# ─── Empty-state guard ───────────────────────────────────────────────────────


def test_memory_paths_are_allowed_without_a_selected_project(roots, monkeypatch):
    """An employee can read its own registries before any folder is chosen."""
    config.set_memory_root("marketer", str(roots["project"]))
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)
    assert te.workspace_guard_error("read_file", {"path": "memory:registry.md"}) is None
    assert te.workspace_guard_error("propose_file", {"path": "memory:journal.md"}) is None
    assert te.workspace_guard_error("list_files", {"subdir": "memory:projects"}) is None


def test_project_paths_stay_blocked_without_a_selected_project(roots, monkeypatch):
    config.set_memory_root("marketer", str(roots["project"]))
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)
    blocked = te.workspace_guard_error("read_file", {"path": "README.md"})
    assert blocked is not None
    assert blocked.get("workspace_not_selected") is True


def test_mixed_targets_stay_blocked_without_a_selected_project(roots, monkeypatch):
    """One project target in the batch is enough to require a folder."""
    config.set_memory_root("marketer", str(roots["project"]))
    monkeypatch.setattr(config, "WORKSPACE_SELECTED", False)
    blocked = te.workspace_guard_error(
        "search_in_files", {"files": ["memory:registry.md", "README.md"]})
    assert blocked is not None


def test_addresses_memory_helper(roots):
    config.set_memory_root("marketer", str(roots["project"]))
    assert te._addresses_memory({"path": "memory:a.md"}) is True
    assert te._addresses_memory({"path": "a.md"}) is False
    assert te._addresses_memory({}) is False
    assert te._addresses_memory(None) is False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
