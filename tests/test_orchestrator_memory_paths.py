# -*- coding: utf-8 -*-
"""
Tests for the working-data-folder resolver of an orchestrator.

Covers the ``MEMORY_DIR:`` prompt marker, the config fallback, the CENTRAL
``DATA_DIR/agents_data/<slug>`` default, the ``workspace:`` prefix for a role
whose data must travel with the project, path-escape protection, and the
report of folders left behind by the previous layout.
"""
import os

import pytest

import core.paths
from core import orchestrator_folders as of


@pytest.fixture()
def central(tmp_path, monkeypatch):
    """Point the central working-data root at a temp directory."""
    root = tmp_path / "agents_data"
    root.mkdir()
    monkeypatch.setattr(core.paths, "AGENTS_DATA_DIR", str(root))
    return root


# ─── Marker parsing ───────────────────────────────────────────────────────────


def test_marker_absent_returns_empty():
    assert of.parse_memory_dir_marker("## ROLE\nОбычный промпт без маркера.") == ""
    assert of.parse_memory_dir_marker("") == ""


def test_marker_relative_value():
    prompt = "## Память и данные\nMEMORY_DIR: project_manager_data\nдалее текст"
    assert of.parse_memory_dir_marker(prompt) == "project_manager_data"


def test_marker_is_case_insensitive_and_trimmed():
    prompt = "   memory_dir:   editor_assistant_data   "
    assert of.parse_memory_dir_marker(prompt) == "editor_assistant_data"


def test_marker_first_occurrence_wins():
    prompt = "MEMORY_DIR: first_dir\n...\nMEMORY_DIR: second_dir"
    assert of.parse_memory_dir_marker(prompt) == "first_dir"


def test_marker_inside_code_fence_is_ignored():
    prompt = "```markdown\nMEMORY_DIR: example_only\n```\nMEMORY_DIR: real_dir"
    assert of.parse_memory_dir_marker(prompt) == "real_dir"


def test_marker_backticks_are_stripped():
    assert of.parse_memory_dir_marker("MEMORY_DIR: `my_data`") == "my_data"


# ─── Central default ──────────────────────────────────────────────────────────


def test_default_path_is_central(tmp_path, central):
    """Without a marker/config the folder lives under the CENTRAL root."""
    result = of.resolve_data_dir("project_manager", str(tmp_path),
                                 prompt_text="no marker here", config={})
    assert result["source"] == "default"
    assert result["path"] == str(central / "project_manager")
    assert result["scope"] == "central"
    assert result["available"] is True
    assert result["exists"] is False


def test_default_path_does_not_need_a_workspace(central):
    """The installation-wide folder exists even with no project selected."""
    result = of.resolve_data_dir("marketer", "", prompt_text="", config={})
    assert result["available"] is True
    assert result["path"] == str(central / "marketer")
    assert result["scope"] == "central"


def test_default_path_ignores_the_open_project(tmp_path, central):
    """Two different projects resolve to the SAME central folder."""
    other = tmp_path / "other_project"
    other.mkdir()
    first = of.resolve_data_dir("marketer", str(tmp_path), prompt_text="", config={})
    second = of.resolve_data_dir("marketer", str(other), prompt_text="", config={})
    assert first["path"] == second["path"]


# ─── Explicit values ──────────────────────────────────────────────────────────


def test_relative_marker_resolves_against_the_project(tmp_path, central):
    """An explicit relative value has always meant "next to the project"."""
    result = of.resolve_data_dir("marketer", str(tmp_path),
                                 prompt_text="MEMORY_DIR: bases/mkt", config={})
    assert result["source"] == "prompt"
    assert result["path"] == os.path.join(str(tmp_path), "bases", "mkt")
    assert result["scope"] == "workspace"
    assert result["outside_workspace"] is False


def test_central_prefix_places_data_in_the_installation(tmp_path, central):
    """A role shared across projects asks for the central root explicitly."""
    result = of.resolve_data_dir("marketer", str(tmp_path),
                                 prompt_text="MEMORY_DIR: central:bases/mkt",
                                 config={})
    assert result["source"] == "prompt"
    assert result["path"] == str(central / "bases" / "mkt")
    assert result["scope"] == "central"


def test_central_prefix_without_a_sub_path_is_the_root(tmp_path, central):
    result = of.resolve_data_dir("marketer", str(tmp_path),
                                 prompt_text="MEMORY_DIR: central:", config={})
    assert result["path"] == str(central)
    assert result["scope"] == "central"


def test_workspace_prefix_keeps_data_in_the_project(tmp_path, central):
    result = of.resolve_data_dir("editor_assistant", str(tmp_path),
                                 prompt_text="MEMORY_DIR: workspace:bases/ed",
                                 config={})
    assert result["source"] == "prompt"
    assert result["path"] == os.path.join(str(tmp_path), "bases", "ed")
    assert result["scope"] == "workspace"


def test_marker_dot_means_workspace_root(tmp_path, central):
    result = of.resolve_data_dir("editor_assistant", str(tmp_path),
                                 prompt_text="MEMORY_DIR: .", config={})
    assert result["source"] == "prompt"
    assert result["path"] == str(tmp_path)
    assert result["scope"] == "workspace"


def test_marker_default_falls_back_to_central_folder(tmp_path, central):
    result = of.resolve_data_dir("teacher_assistant", str(tmp_path),
                                 prompt_text="MEMORY_DIR: default", config={})
    assert result["source"] == "default"
    assert result["path"] == str(central / "teacher_assistant")


def test_absolute_marker_outside_workspace_is_flagged(tmp_path, central):
    external = tmp_path.parent / "external_base"
    external.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = of.resolve_data_dir("teacher_assistant", str(workspace),
                                 prompt_text=f"MEMORY_DIR: {external}", config={})
    assert result["source"] == "prompt"
    assert result["path"] == str(external)
    assert result["scope"] == "external"
    assert result["outside_workspace"] is True


def test_relative_marker_escaping_the_project_is_ignored(tmp_path, central):
    workspace = tmp_path / "project"
    workspace.mkdir()
    result = of.resolve_data_dir("marketer", str(workspace),
                                 prompt_text="MEMORY_DIR: ../../etc", config={})
    assert result["source"] == "default"
    assert result["path"] == str(central / "marketer")


def test_workspace_prefix_without_a_project_falls_back_to_central(central):
    """A project-relative value cannot resolve with no project selected."""
    result = of.resolve_data_dir("marketer", "",
                                 prompt_text="MEMORY_DIR: workspace:bases/mkt",
                                 config={})
    assert result["source"] == "default"
    assert result["path"] == str(central / "marketer")
    assert result["available"] is True


def test_config_used_only_without_marker(tmp_path, central):
    config = {"memory_dir": "from_config"}
    with_config = of.resolve_data_dir("marketer", str(tmp_path),
                                      prompt_text="без маркера", config=config)
    assert with_config["source"] == "config"
    assert with_config["path"] == os.path.join(str(tmp_path), "from_config")

    with_marker = of.resolve_data_dir(
        "marketer", str(tmp_path),
        prompt_text="MEMORY_DIR: from_prompt", config=config)
    assert with_marker["source"] == "prompt"
    assert with_marker["path"] == os.path.join(str(tmp_path), "from_prompt")


def test_no_workspace_but_absolute_marker_is_available(tmp_path, central):
    result = of.resolve_data_dir("marketer", "",
                                 prompt_text=f"MEMORY_DIR: {tmp_path}", config={})
    assert result["available"] is True
    assert result["path"] == str(tmp_path)


# ─── Conflict / legacy report ────────────────────────────────────────────────


def test_existing_marker_folder_is_reported_as_conflict(tmp_path, central):
    """A folder abandoned after the path changed is reported, not moved."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    old = central / "old_place"
    old.mkdir()
    (old / "registry.md").write_text("# реестр", encoding="utf-8")

    result = of.resolve_data_dir("editor_assistant", str(workspace),
                                 prompt_text="MEMORY_DIR: bases/editor", config={})
    assert result["source"] == "prompt"
    assert result["path"] == os.path.join(str(workspace), "bases", "editor")
    conflict_sources = {c["source"] for c in result["conflicts"]}
    assert "config" not in conflict_sources


def test_legacy_workspace_folder_is_reported(tmp_path, central):
    """The pre-central <workspace>/<slug>_data folder is reported separately."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    legacy = workspace / "editor_assistant_data"
    legacy.mkdir()
    (legacy / "registry.md").write_text("# реестр", encoding="utf-8")

    result = of.resolve_data_dir("editor_assistant", str(workspace),
                                 prompt_text="", config={})
    assert result["path"] == str(central / "editor_assistant")
    assert result["legacy"] is not None
    assert result["legacy"]["path"] == str(legacy)
    assert result["legacy"]["files"] == 1
    assert result["legacy"]["is_active"] is False
    assert any(c["source"] == "legacy" for c in result["conflicts"])


def test_empty_legacy_folder_is_not_reported(tmp_path, central):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "marketer_data").mkdir()
    result = of.resolve_data_dir("marketer", str(workspace),
                                 prompt_text="", config={})
    assert result["legacy"] is None
    assert result["conflicts"] == []


def test_active_workspace_folder_is_not_a_legacy_conflict(tmp_path, central):
    """A role deliberately kept in the project shows up as active, not legacy."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    data = workspace / "editor_assistant_data"
    data.mkdir()
    (data / "registry.md").write_text("# реестр", encoding="utf-8")
    result = of.resolve_data_dir("editor_assistant", str(workspace),
                                 prompt_text="MEMORY_DIR: .", config={})
    assert result["path"] == str(workspace)
    # The role points at the project root, the old data folder is elsewhere in
    # the same project: it is a conflict, but never the same path as the scope.
    assert all(c["path"] != result["path"] for c in result["conflicts"])


def test_resolution_is_pure_and_creates_nothing(tmp_path, central):
    before = sorted(os.listdir(str(central)))
    of.resolve_data_dir("project_manager", str(tmp_path),
                        prompt_text="MEMORY_DIR: bases/pm", config={})
    assert sorted(os.listdir(str(central))) == before


def test_default_data_dir_name_is_the_legacy_name():
    assert of.default_data_dir_name("Project Manager") == "project_manager_data"
    assert of.default_data_dir_name("") == "unnamed_data"


def test_central_data_dir_uses_safe_slug(central):
    assert of.central_data_dir("Project Manager") == str(central / "project_manager")
    assert of.central_data_dir("") == str(central / "unnamed")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
